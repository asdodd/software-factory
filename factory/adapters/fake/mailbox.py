from __future__ import annotations

from collections.abc import Mapping

from factory.ports.context import AdapterContext
from factory.ports.errors import NotFound
from factory.ports.types import Cursor, MailMessage, MailReply, MailThread, Page


class FakeMailbox:
    def __init__(self) -> None:
        self.messages: list[MailMessage] = []
        self.processed: set[str] = set()
        self.sent: dict[str, MailReply] = {}

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> FakeMailbox:
        return cls()

    def deliver(self, message: MailMessage) -> None:
        self.messages.append(message)

    def poll(self, cursor: Cursor | None = None) -> Page[MailMessage]:
        start = int(cursor or 0)
        fresh = tuple(m for m in self.messages[start:] if m.message_id not in self.processed)
        return Page(fresh, str(len(self.messages)))

    def get_thread(self, thread_id: str) -> MailThread:
        msgs = tuple(m for m in self.messages if m.thread_id == thread_id)
        if not msgs:
            raise NotFound(f"thread {thread_id}")
        return MailThread(thread_id, msgs)

    def send_reply(self, reply: MailReply) -> str:
        self.sent.setdefault(reply.idempotency_key, reply)
        return f"<reply-{reply.idempotency_key}@sim.invalid>"

    def mark_processed(self, message_id: str) -> None:
        self.processed.add(message_id)
