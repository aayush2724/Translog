"""Threading — does this email belong to a request we already have?"""

from translog_quote.domain.conversation.model import (
    CorrelationPolicy,
    CorrelationResult,
    NewRequest,
    Thread,
)

__all__ = ["CorrelationPolicy", "CorrelationResult", "NewRequest", "Thread"]
