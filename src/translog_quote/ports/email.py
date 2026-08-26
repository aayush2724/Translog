"""Inbound and outbound mail."""

from __future__ import annotations

from typing import Protocol

from translog_quote.domain.email import OutboundMessage, RawEmail


class EmailSource(Protocol):
    """Where inbound messages come from.

    Understands mail, not cargo. It must not parse shipment fields, and it must not
    call a model — a regex that extracts "500 kg" belongs in extraction or nowhere.
    """

    def fetch_new(self) -> tuple[RawEmail, ...]: ...


class EmailSink(Protocol):
    """Where outbound messages go — clarifications and quotations."""

    def send(self, message: OutboundMessage) -> None: ...
