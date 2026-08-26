"""Failure taxonomy (docs/architecture.md §12).

Business *outcomes* are result types, not exceptions: an incomplete shipment, an
empty eligible-rate set, a maker rejection and a client decline are all the
workflow behaving correctly. Only the classes below are raised, and each one means
the surrounding infrastructure or a contract is broken.
"""

from translog_quote.errors.taxonomy import (
    ContractViolation,
    IllegalTransition,
    PermanentFailure,
    TransientFailure,
    TranslogError,
    UnresolvedFieldMapping,
)

__all__ = [
    "ContractViolation",
    "IllegalTransition",
    "PermanentFailure",
    "TransientFailure",
    "TranslogError",
    "UnresolvedFieldMapping",
]
