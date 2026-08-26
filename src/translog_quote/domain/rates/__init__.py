"""The rate pipeline's vocabulary.

Four stages, four types (docs/architecture.md §9):

    raw payload  --RateMapper-->  Rate[]  --FilterChain-->  FilterOutcome
                 --RateSelector-->  Selection

Each stage has a property the others must not have: mapping changes shape but
never membership, filtering changes membership but never order and never scores,
selection changes order but never membership. They are separate components so each
can be tested for the property it is supposed to preserve.
"""

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
from translog_quote.domain.rates.strategy import (
    FASTEST_ELIGIBLE,
    Selection,
    SelectionStrategy,
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
    "SortKey",
    "SortOrder",
    "TransitTime",
    "TransitUnit",
]
