"""The audit trail's vocabulary.

Distinct from the application log, and conflating the two is a mistake worth
naming. The application log is developer diagnostics — freeform and discardable.
The audit trail is append-only evidence that the workflow ran as designed, and it
is a deliverable of the demo: it is how a stakeholder verifies the AI did not
decide anything it was not supposed to.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict


class AuditEventType(StrEnum):
    EMAIL_RECEIVED = "email_received"
    EXTRACTION_CALLED = "extraction_called"
    RECORD_MERGED = "record_merged"
    VALIDATED = "validated"
    CLARIFICATION_SENT = "clarification_sent"
    CONFLICT_DETECTED = "conflict_detected"
    RATES_FETCHED = "rates_fetched"
    RATES_NORMALIZED = "rates_normalized"
    RATES_FILTERED = "rates_filtered"
    RATE_SELECTED = "rate_selected"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    QUOTATION_SENT = "quotation_sent"
    CLIENT_RESPONDED = "client_responded"
    STATE_CHANGED = "state_changed"
    FAILED = "failed"


class AuditEvent(BaseModel):
    """One entry. Append-only; entries are never edited or removed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    event: AuditEventType
    at: datetime
    detail: dict[str, Any] = {}


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...
