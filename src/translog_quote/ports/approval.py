"""The human approval boundary."""

from __future__ import annotations

from typing import Protocol

from translog_quote.domain.quotation import ApprovalDecision, ReviewPacket


class ApprovalPort(Protocol):
    """Where the quotation maker decides.

    This is a halt, not a callback. The pipeline enters PENDING_APPROVAL and stops;
    resuming requires an explicit decision. No implementation may return a default
    on a timeout (BR-11).
    """

    def request(self, review: ReviewPacket) -> ApprovalDecision: ...
