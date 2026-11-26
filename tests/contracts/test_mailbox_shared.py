"""Shared Mailbox contract against the fake mailbox and the IMAP/SMTP adapter
(on in-process fake servers)."""

from datetime import UTC, datetime

import pytest

from factory.adapters.fake.mailbox import FakeMailbox
from factory.adapters.mail.imap_smtp import ImapSmtpMailbox
from factory.ports.types import MailAttachment, MailMessage, MailReply
from tests.fixtures.mail_servers import FakeImapServer, FakeSmtpServer, message

T = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


class FakeHarness:
    def __init__(self):
        self.box = FakeMailbox()

    def deliver(self, sender, subject, body, msg_id, references="", attachments=()):
        thread = references.split()[0] if references else msg_id
        self.box.deliver(MailMessage(msg_id, thread, None, sender, ("factory@sim.invalid",), (), subject, body, {}, T,
                                     tuple(MailAttachment(n, "text/plain", d) for n, d in attachments)))

    def sent(self):
        return [(list(r.to), r.subject, r.body) for r in self.box.sent.values()]


class ImapHarness:
    def __init__(self):
        self.imap, self.smtp = FakeImapServer(), FakeSmtpServer()
        self.box = ImapSmtpMailbox("factory@sim.invalid", self.imap.connect, self.smtp.connect, "factory@sim.invalid",
                                   "mail-pass-123456", "INBOX", "Processed")

    def deliver(self, sender, subject, body, msg_id, references="", attachments=()):
        self.imap.deliver(message(sender, subject, body, msg_id, references=references, attachments=attachments))

    def sent(self):
        return [([m["To"]], m["Subject"], m.get_content().strip()) for m in self.smtp.sent]


@pytest.fixture(params=["fake", "imap"])
def h(request):
    return FakeHarness() if request.param == "fake" else ImapHarness()


def test_poll_thread_attachments_and_processing(h):
    h.deliver("Alice <alice@sim.invalid>", "Need multiply", "Please.", "<m1@sim.invalid>",
              attachments=[("log.txt", b"ERROR")])
    h.deliver("bob@sim.invalid", "Re: Need multiply", "+1", "<m2@sim.invalid>", references="<m1@sim.invalid>")
    msgs = h.box.poll().items
    assert [m.message_id for m in msgs] == ["<m1@sim.invalid>", "<m2@sim.invalid>"]
    assert msgs[1].thread_id == "<m1@sim.invalid>" and msgs[0].attachments[0].content == b"ERROR"
    thread = h.box.get_thread("<m1@sim.invalid>")
    assert [m.message_id for m in thread.messages] == ["<m1@sim.invalid>", "<m2@sim.invalid>"]
    h.box.mark_processed("<m1@sim.invalid>")
    assert [m.message_id for m in h.box.poll().items] == ["<m2@sim.invalid>"]


def test_send_reply(h):
    h.box.send_reply(MailReply("<m1@sim.invalid>", "<m1@sim.invalid>", ("alice@sim.invalid",), (), "Re: x",
                               "Created SIM-1", "k1"))
    assert h.sent() == [(["alice@sim.invalid"], "Re: x", "Created SIM-1")]


def test_imap_specifics():
    h = ImapHarness()
    rid = h.box.send_reply(MailReply("<m1@sim.invalid>", "<m1@sim.invalid>", ("alice@sim.invalid",),
                                     ("bob@sim.invalid",), "Re: x", "body", "k1"))
    again = h.box.send_reply(MailReply("<m1@sim.invalid>", "<m1@sim.invalid>", ("alice@sim.invalid",), (), "Re: x",
                                       "body", "k1"))
    msg = h.smtp.sent[0]
    assert rid == again and msg["Auto-Submitted"] == "auto-replied" and msg["In-Reply-To"] == "<m1@sim.invalid>"
    assert msg["Cc"] == "bob@sim.invalid" and rid.endswith("@sim.invalid>")
    h.deliver("alice@sim.invalid", "x", "y", "<m9@sim.invalid>")
    h.box.poll()
    h.box.mark_processed("<m9@sim.invalid>")
    assert list(h.imap.folders["Processed"]) and not h.imap.folders["INBOX"]
