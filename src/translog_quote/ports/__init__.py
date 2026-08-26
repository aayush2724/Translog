"""Ports (L1) — interfaces only. No implementations live here, ever.

Each port is the single place its technology is allowed to appear behind. What
crosses a port is a *domain type*, never a wire type: ExtractionPort returns
ExtractedFields rather than a chat-completion object, RateSearchPort returns Rate
objects rather than a WebCargo response. That is what lets the real adapters be
written later without a change inside `domain`.

Mapping to the conceptual names used in the Phase 1 brief:

    EmailProvider        -> EmailSource (inbound) + EmailSink (outbound)
    ShipmentExtractor    -> ExtractionPort
    WebCargoRateProvider -> RateSearchPort
    QuotationMessenger   -> EmailSink

Three further ports exist for determinism rather than integration: ClockPort,
StorePort and ApprovalPort.
"""

from translog_quote.ports.approval import ApprovalPort
from translog_quote.ports.clock import ClockPort
from translog_quote.ports.email import EmailSink, EmailSource
from translog_quote.ports.extraction import ExtractionPort
from translog_quote.ports.rates import RateSearchPort
from translog_quote.ports.store import StorePort

__all__ = [
    "ApprovalPort",
    "ClockPort",
    "EmailSink",
    "EmailSource",
    "ExtractionPort",
    "RateSearchPort",
    "StorePort",
]
