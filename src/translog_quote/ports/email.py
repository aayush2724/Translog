"""Inbound and outbound mail."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from translog_quote.domain.email import OutboundMessage, RawEmail

if TYPE_CHECKING:
    from datetime import datetime


class EmailSource(Protocol):
    """Where inbound messages come from.

    Understands mail, not cargo. It must not parse shipment fields, and it must not
    call a model — a regex that extracts "500 kg" belongs in extraction or nowhere.
    """

    def fetch_new(self, *, since: datetime | None = None) -> tuple[RawEmail, ...]:
        """Inbound messages to consider this poll.

        ``since`` is the operations-mode watermark: implementations that can
        bound the read by date should fetch messages received at or after it
        (minus a safe overlap), oldest-first. ``None`` means no bound — the
        original newest-first slice — which is what demonstration mode and the
        fixtures use."""
        ...


class EmailSink(Protocol):
    """Where outbound messages go — clarifications and quotations."""

    def send(self, message: OutboundMessage) -> None: ...
