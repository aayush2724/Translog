"""Thread types and the correlation contract."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from translog_quote.domain.email import RawEmail


class Thread(BaseModel):
    """Every message seen for one request, in arrival order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    message_ids: tuple[str, ...] = ()


class NewRequest:
    """Sentinel: this email starts a new request rather than continuing one."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "NewRequest()"


class AmbiguousCorrelation:
    """Sentinel: the headers place this email in more than one known request.

    Distinct from ``NewRequest`` because the two demand opposite handling. A
    ``NewRequest`` is safe to start; an ambiguous reply is not safe to do
    anything automatic with — it is real correspondence about an existing
    shipment, and picking one of the candidates would merge a client's answer
    into the wrong record, where it would validate and be quoted. It goes to a
    person.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "AmbiguousCorrelation()"


CorrelationResult = str | NewRequest | AmbiguousCorrelation
"""An existing ``request_id``, ``NewRequest``, or a refusal to choose."""


class CorrelationPolicy(Protocol):
    """How a reply is matched to an existing request.

    A named policy rather than inline logic, because the strategy is unconfirmed
    (AMB-11). Correlation never guesses: an email that cannot be placed becomes a
    new request or is routed to manual review, and is never merged into a maybe.
    """

    def correlate(self, email: RawEmail, threads: tuple[Thread, ...]) -> CorrelationResult: ...
