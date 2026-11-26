from __future__ import annotations

from typing import Protocol, runtime_checkable

from factory.ports.types import Cursor, MailMessage, MailReply, MailThread, Page


@runtime_checkable
class Mailbox(Protocol):
    def poll(self, cursor: Cursor | None = None) -> Page[MailMessage]:
        """Unprocessed messages addressed to the bot."""
        ...

    def get_thread(self, thread_id: str) -> MailThread: ...

    def send_reply(self, reply: MailReply) -> str:
        """Returns message id. Idempotent on reply.idempotency_key."""
        ...

    def mark_processed(self, message_id: str) -> None: ...
