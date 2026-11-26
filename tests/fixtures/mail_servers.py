"""In-process fake IMAP and SMTP servers with the imaplib/smtplib method
subset the mailbox adapter uses."""

from __future__ import annotations

import email
import email.policy
from email.message import EmailMessage


class FakeImapServer:
    def __init__(self) -> None:
        self.folders: dict[str, dict[bytes, dict]] = {"INBOX": {}, "Processed": {}}
        self.logins: list[tuple[str, str]] = []
        self._uid = 0

    def deliver(self, msg: EmailMessage, folder: str = "INBOX") -> None:
        self._uid += 1
        self.folders[folder][str(self._uid).encode()] = {"raw": msg.as_bytes(), "flags": set()}

    def connect(self) -> FakeImapConnection:
        return FakeImapConnection(self)


class FakeImapConnection:
    def __init__(self, server: FakeImapServer) -> None:
        self.server = server
        self.folder = "INBOX"

    def login(self, user, password):
        self.server.logins.append((user, password))
        return "OK", [b"logged in"]

    def select(self, folder):
        self.folder = folder
        return "OK", [str(len(self.server.folders[folder])).encode()]

    def logout(self):
        return "BYE", [b""]

    def uid(self, command, *args):
        box = self.server.folders[self.folder]
        command = command.upper()
        if command == "SEARCH":
            criteria = [a for a in args if a is not None]
            if criteria == ["UNSEEN"]:
                hits = [u for u, m in box.items() if "\\Seen" not in m["flags"]]
            else:
                needle = criteria[3]
                hits = []
                for folder in self.server.folders.values():
                    for u, m in folder.items():
                        msg = email.message_from_bytes(m["raw"], policy=email.policy.default)
                        if needle in str(msg.get("Message-ID", "")) or needle in str(msg.get("References", "")):
                            hits.append(u)
            return "OK", [b" ".join(hits)]
        if command == "FETCH":
            uid = args[0]
            for folder in (box, *self.server.folders.values()):
                if uid in folder:
                    return "OK", [(b"1 (UID " + uid + b" BODY[] {n}", folder[uid]["raw"]), b")"]
            return "NO", [None]
        if command == "STORE":
            box[args[0]]["flags"].add("\\Seen")
            return "OK", [b""]
        if command == "MOVE":
            self.server.folders[args[1]][args[0]] = box.pop(args[0])
            return "OK", [b""]
        raise AssertionError(f"unexpected IMAP command {command}")


class FakeSmtpServer:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def connect(self) -> FakeSmtpConnection:
        return FakeSmtpConnection(self)


class FakeSmtpConnection:
    def __init__(self, server: FakeSmtpServer) -> None:
        self.server = server

    def login(self, user, password):
        return 235, b"ok"

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.server.sent.append(msg)
        return {}

    def quit(self):
        return 221, b"bye"


def message(sender: str, subject: str, body: str, msg_id: str, to: str = "factory@sim.invalid", cc: str = "",
            references: str = "", in_reply_to: str = "", attachments=(), headers=None) -> EmailMessage:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = sender, to, subject, msg_id
    m["Date"] = "Mon, 05 Jan 2026 09:00:00 +0000"
    if cc:
        m["Cc"] = cc
    if references:
        m["References"] = references
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    for name, data in attachments:
        m.add_attachment(data, maintype="text", subtype="plain", filename=name)
    return m
