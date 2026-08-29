"""Turning an explicit choice into an approval decision.

A domain rule, not an interface detail: *what counts as a decision* is a
business question, and the answer is the same whether it arrives from a
terminal prompt, an HTTP request, or anything built later.

Two words, both required to be exact, and a name that cannot be omitted. There
is deliberately no boolean form of this function — a missing `approved=True`
would arrive as `False` and read as a decline, which is a way to record someone
as having refused something they never saw.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.domain.quotation.model import Approved, Rejected

if TYPE_CHECKING:
    from datetime import datetime

    from translog_quote.domain.quotation.model import ApprovalDecision


class NotADecision(ValueError):
    """What arrived is not an approval and not a decline.

    Deliberately an error rather than a fallback, and local to `domain` for the
    same reason `UnknownPlace` is: a domain rule states what it will not accept
    without depending on the failure taxonomy above it. Resolving an
    unrecognised input to either outcome would either send a quotation nobody
    authorised or record a refusal nobody made.
    """


#: The two words a caller may send. Spelled out rather than inferred.
APPROVE = "approve"
DECLINE = "decline"


def decision_from_choice(
    choice: str, *, by: str, at: datetime, reason: str = ""
) -> ApprovalDecision:
    """One explicit choice into an `ApprovalDecision`, or a refusal.

    Deliberately strict. An unrecognised, empty or missing choice raises rather
    than resolving to either outcome: defaulting to approval would send a
    quotation nobody authorised, and defaulting to decline would record a
    person as having refused something they never saw.
    """
    who = by.strip()
    if not who:
        raise NotADecision(
            "An approval decision must name the person making it. "
            "Decisions are never recorded anonymously."
        )

    normalised = choice.strip().lower()
    if normalised == APPROVE:
        return Approved(by=who, at=at)
    if normalised == DECLINE:
        return Rejected(by=who, at=at, reason=reason.strip())
    raise NotADecision(
        f"{choice!r} is not a decision. Send exactly {APPROVE!r} or {DECLINE!r} — "
        "there is no default."
    )
