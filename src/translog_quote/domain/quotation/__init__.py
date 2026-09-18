"""Quotation, the review packet, and the approval gate's vocabulary."""

from translog_quote.domain.quotation.compose import (
    INTERNAL_SUBJECT_PREFIX,
    SIMULATED_RATE_NOTICE,
    IncompatibleService,
    build_quotation,
    compose_no_rates_body,
    compose_no_rates_subject,
    compose_quotation_body,
    compose_quotation_subject,
    compose_review_body,
    compose_review_request,
    compose_review_subject,
    no_rates_message,
    quotation_message,
)
from translog_quote.domain.quotation.decide import (
    APPROVE,
    DECLINE,
    NotADecision,
    decision_from_choice,
)
from translog_quote.domain.quotation.model import (
    ApprovalDecision,
    Approved,
    Quotation,
    Rejected,
    ReviewPacket,
)

__all__ = [
    "IncompatibleService",
    "APPROVE",
    "DECLINE",
    "INTERNAL_SUBJECT_PREFIX",
    "NotADecision",
    "SIMULATED_RATE_NOTICE",
    "ApprovalDecision",
    "Approved",
    "Quotation",
    "Rejected",
    "ReviewPacket",
    "build_quotation",
    "compose_no_rates_body",
    "compose_no_rates_subject",
    "compose_quotation_body",
    "compose_quotation_subject",
    "compose_review_body",
    "compose_review_request",
    "compose_review_subject",
    "decision_from_choice",
    "no_rates_message",
    "quotation_message",
]
