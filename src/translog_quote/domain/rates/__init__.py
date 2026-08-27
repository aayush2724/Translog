"""The rate pipeline's vocabulary.

Four stages, four types (docs/architecture.md §9):

    raw payload  --RateMapper-->  Rate[]  --FilterChain-->  FilterOutcome
                 --RateSelector-->  Selection

Each stage has a property the others must not have: mapping changes shape but
never membership, filtering changes membership but never order and never scores,
selection changes order but never membership. They are separate components so each
can be tested for the property it is supposed to preserve.
"""

from translog_quote.domain.rates.filters import (
    drop_incomplete_rate,
    drop_restricted_carrier,
    drop_unrankable_rate,
    filter_rates,
)
from translog_quote.domain.rates.model import (
    ExcludedRate,
    ExclusionReason,
    FilterOutcome,
    Rate,
    RateQuery,
    RateRestrictions,
    RateSearchResult,
    TransitTime,
    TransitUnit,
)
from translog_quote.domain.rates.selection import select_rate
from translog_quote.domain.rates.strategy import (
    FASTEST_ELIGIBLE,
    Selection,
    SelectionStrategy,
    SortField,
    SortKey,
    SortOrder,
)

__all__ = [
    "FASTEST_ELIGIBLE",
    "ExcludedRate",
    "ExclusionReason",
    "FilterOutcome",
    "Rate",
    "RateQuery",
    "RateRestrictions",
    "RateSearchResult",
    "Selection",
    "SelectionStrategy",
    "SortField",
    "SortKey",
    "SortOrder",
    "TransitTime",
    "TransitUnit",
    "drop_incomplete_rate",
    "drop_restricted_carrier",
    "drop_unrankable_rate",
    "filter_rates",
    "select_rate",
]
