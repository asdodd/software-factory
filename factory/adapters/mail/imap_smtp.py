"""IMAP/SMTP mailbox (stdlib imaplib, smtplib, email). Written from the RFCs
and exercised only through the Mailbox contract suite against in-process fake
IMAP and SMTP servers. Never run against a live mail server in this build.

- poll(): UNSEEN messages in the configured folder, parsed (headers, text body,
  attachments). Threads are keyed by the root Message-ID (first References id,
  else In-Reply-To, else the message's own id).
- mark_processed(): flags \\Seen and, if processed_folder is set, moves it there.
- send_reply(): sets In-Reply-To/References, Auto-Submitted: auto-replied (so
  other systems do not answer the bot), and a Message-ID derived from the
  idempotency key, so a resend is recognizable as the same message."""

from __future__ import annotations

import email
import email.policy
import hashlib
import imaplib
import re
import smtplib
import ssl
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import make_msgid, parsedate_to_datetime

from factory.ports.context import AdapterContext
from factory.ports.errors import InvalidRequest, NotFound, Unavailable
from factory.ports.types import Cursor, MailAttachment, MailMessage, MailReply, MailThread, Page

IDS = re.compile(r"<[^<>\s]+>")


def thread_root(msg) -> str:
    refs = IDS.findall(str(msg.get("References", "")))
    if refs:
        return refs[0]
    reply = IDS.findall(str(msg.get("In-Reply-To", "")))
    return reply[0] if reply else str(msg.get("Message-ID", "")).strip()


def parse(raw: bytes) -> MailMessage:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body is not None else ""
    attachments = []
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True) or b""
        attachments.append(MailAttachment(part.get_filename() or "attachment", part.get_content_type(), payload))
    try:
        received = parsedate_to_datetime(str(msg.get("Date"))) if msg.get("Date") else datetime.now(UTC)
    except (TypeError, ValueError):
        received = datetime.now(UTC)
    split = lambda h: tuple(a.strip() for a in str(msg.get(h, "")).split(",") if a.strip())  # noqa: E731
    reply_to = IDS.findall(str(msg.get("In-Reply-To", "")))
    return MailMessage(str(msg.get("Message-ID", "")).strip(), thread_root(msg), reply_to[0] if reply_to else None,
                       str(msg.get("From", "")), split("To"), split("Cc"), str(msg.get("Subject", "")), text,
                       {k: str(v) for k, v in msg.items()}, received, tuple(attachments))


class ImapSmtpMailbox:
    def __init__(self, address: str, imap_factory: Callable[[], object], smtp_factory: Callable[[], object],
                 username: str, password: str, folder: str = "INBOX", processed_folder: str = "") -> None:
        self.address = address
        self.imap_factory = imap_factory
        self.smtp_factory = smtp_factory
        self.username = username
        self.password = password
        self.folder = folder
        self.processed_folder = processed_folder
        self._uids: dict[str, bytes] = {}
        self._seen: dict[str, MailMessage] = {}

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext,
                      imap_factory: Callable[[], object] | None = None,
                      smtp_factory: Callable[[], object] | None = None) -> ImapSmtpMailbox:
        address = str(settings.get("address", ""))
        if not address:
            raise InvalidRequest("plugins.mailbox.settings.address is required")
        password = ctx.secrets.resolve(settings["password"], "mail password")
        context = ssl.create_default_context(cafile=str(settings.get("ca_bundle_path", "")) or None)
        timeout = float(settings.get("timeout_seconds", 30))
        imap_host, smtp_host = str(settings["imap_host"]), str(settings["smtp_host"])
        imap_port, smtp_port = int(settings.get("imap_port", 993)), int(settings.get("smtp_port", 587))

        def imap():
            return imaplib.IMAP4_SSL(imap_host, imap_port, ssl_context=context, timeout=timeout)

        def smtp():
            conn = smtplib.SMTP(smtp_host, smtp_port, timeout=timeout)
            conn.starttls(context=context)
            return conn

        return cls(address, imap_factory or imap, smtp_factory or smtp, str(settings.get("username", address)),
                   password, str(settings.get("folder", "INBOX")), str(settings.get("processed_folder", "")))

    def _session(self):
        try:
            conn = self.imap_factory()
            conn.login(self.username, self.password)
            conn.select(self.folder)
            return conn
        except (OSError, imaplib.IMAP4.error) as exc:
            raise Unavailable(f"IMAP: {type(exc).__name__}") from None

    def _fetch(self, conn, uids: list[bytes]) -> list[tuple[bytes, MailMessage]]:
        out = []
        for uid in uids:
            status, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if status != "OK" or not data or not isinstance(data[0], tuple):
                continue
            out.append((uid, parse(data[0][1])))
        return out

    def poll(self, cursor: Cursor | None = None) -> Page[MailMessage]:
        conn = self._session()
        try:
            status, data = conn.uid("SEARCH", None, "UNSEEN")
            uids = data[0].split() if status == "OK" and data and data[0] else []
            messages = []
            for uid, msg in self._fetch(conn, uids):
                self._uids[msg.message_id] = uid
                self._seen[msg.message_id] = msg
                messages.append(msg)
            return Page(tuple(messages), cursor)
        finally:
            conn.logout()

    def get_thread(self, thread_id: str) -> MailThread:
        conn = self._session()
        try:
            status, data = conn.uid("SEARCH", None, "OR", "HEADER", "Message-ID", thread_id,
                                    "HEADER", "References", thread_id)
            uids = data[0].split() if status == "OK" and data and data[0] else []
            found = {m.message_id: m for _, m in self._fetch(conn, uids)}
        finally:
            conn.logout()
        found.update({k: m for k, m in self._seen.items() if m.thread_id == thread_id})
        if not found:
            raise NotFound(f"thread {thread_id}")
        return MailThread(thread_id, tuple(sorted(found.values(), key=lambda m: m.received)))

    def send_reply(self, reply: MailReply) -> str:
        domain = self.address.split("@", 1)[-1]
        message_id = f"<factory-{hashlib.sha256(reply.idempotency_key.encode()).hexdigest()[:24]}@{domain}>"
        msg = EmailMessage()
        msg["From"] = self.address
        msg["To"] = ", ".join(reply.to)
        if reply.cc:
            msg["Cc"] = ", ".join(reply.cc)
        msg["Subject"] = reply.subject
        msg["Message-ID"] = message_id or make_msgid(domain=domain)
        msg["In-Reply-To"] = reply.in_reply_to
        msg["References"] = " ".join(dict.fromkeys(i for i in (reply.thread_id, reply.in_reply_to) if i))
        msg["Auto-Submitted"] = "auto-replied"
        msg["X-Auto-Response-Suppress"] = "All"
        msg.set_content(reply.body)
        try:
            conn = self.smtp_factory()
            try:
                conn.login(self.username, self.password)
                conn.send_message(msg, from_addr=self.address, to_addrs=[*reply.to, *reply.cc])
            finally:
                conn.quit()
        except (OSError, smtplib.SMTPException) as exc:
            raise Unavailable(f"SMTP: {type(exc).__name__}") from None
        return message_id

    def mark_processed(self, message_id: str) -> None:
        uid = self._uids.get(message_id)
        if uid is None:
            return
        conn = self._session()
        try:
            conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            if self.processed_folder:
                conn.uid("MOVE", uid, self.processed_folder)
        finally:
            conn.logout()
