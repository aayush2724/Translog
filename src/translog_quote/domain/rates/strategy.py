"""Selection strategy — the business rule, expressed as data.

Switching the business from fastest to cheapest is a reordering of ``keys`` in
configuration. No code changes (BR-3).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from translog_quote.domain.rates.model import Rate


class SortField(StrEnum):
    TRANSIT = "transit"
    TOTAL_AMOUNT = "total_amount"
    CARRIER_CODE = "carrier_code"


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


class SortKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field: SortField
    order: SortOrder = SortOrder.ASC


class SelectionStrategy(BaseModel):
    """An ordered comparator chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    keys: tuple[SortKey, ...]


FASTEST_ELIGIBLE = SelectionStrategy(
    name="fastest_eligible",
    keys=(
        SortKey(field=SortField.TRANSIT, order=SortOrder.ASC),  # BR-1, primary
        SortKey(field=SortField.TOTAL_AMOUNT, order=SortOrder.ASC),  # BR-2, tie-break
        SortKey(field=SortField.CARRIER_CODE, order=SortOrder.ASC),  # determinism only
    ),
)
"""The current stakeholder requirement.

The third key is not a business rule. It exists so that two otherwise-identical
offers always produce the same winner across runs.
"""


class Selection(BaseModel):
    """The chosen rate, why it won, and what it beat.

    ``reason`` is generated from the winning comparison in Phase 3, not written by
    hand, so the explanation shown to the quotation maker cannot drift from the
    logic that produced it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rate: Rate
    reason: str
    runners_up: tuple[Rate, ...] = ()
