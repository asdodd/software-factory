"""Notifier that alerts people by mentioning them in a ticket comment. The
mention syntax is config (Jira Server/Data Center: "[~{user}]")."""

from __future__ import annotations

from collections.abc import Mapping

from factory.ports.context import AdapterContext
from factory.ports.types import Notification


class TicketCommentNotifier:
    def __init__(self, tickets, mention_format: str) -> None:
        self.tickets = tickets
        self.mention_format = mention_format

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> TicketCommentNotifier:
        return cls(ctx.port("ticket_system"), str(settings.get("mention_format", "@{user}")))

    def notify(self, notification: Notification) -> None:
        if not notification.recipients:
            return
        mentions = " ".join(self.mention_format.format(user=u) for u in notification.recipients)
        body = f"{mentions} {notification.subject}" + (f"\n{notification.link}" if notification.link else "")
        self.tickets.add_comment(notification.ticket_key, body,
                                 f"notify:{notification.kind.value}:{notification.ticket_key}:{notification.link}")
