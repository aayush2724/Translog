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


CorrelationResult = str | NewRequest
"""Either an existing ``request_id`` or ``NewRequest``."""


class CorrelationPolicy(Protocol):
    """How a reply is matched to an existing request.

    A named policy rather than inline logic, because the strategy is unconfirmed
    (AMB-11). Correlation never guesses: an email that cannot be placed becomes a
    new request or is routed to manual review, and is never merged into a maybe.
    """

    def correlate(self, email: RawEmail, threads: tuple[Thread, ...]) -> CorrelationResult: ...
