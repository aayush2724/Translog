"""Threading — does this email belong to a request we already have?"""

from translog_quote.domain.conversation.correlation import HeaderChainCorrelation
from translog_quote.domain.conversation.model import (
    AmbiguousCorrelation,
    CorrelationPolicy,
    CorrelationResult,
    NewRequest,
    Thread,
)

__all__ = [
    "AmbiguousCorrelation",
    "CorrelationPolicy",
    "CorrelationResult",
    "HeaderChainCorrelation",
    "NewRequest",
    "Thread",
]
