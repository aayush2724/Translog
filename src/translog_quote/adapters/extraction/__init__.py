"""adapters.extraction

Implements `ports.ExtractionPort` against OpenRouter.

    build_openrouter_extractor(settings)  ->  OpenRouterExtractionAdapter
                                                  |
                                          HttpxChatTransport  ->  OpenRouter

This is the only package in the codebase that knows a language model exists at
the other end of a socket. The prompt, the schema and the field semantics all
come from `domain.extraction` and are unchanged by anything here.
"""

from translog_quote.adapters.extraction.factory import build_openrouter_extractor
from translog_quote.adapters.extraction.openrouter import OpenRouterExtractionAdapter
from translog_quote.adapters.extraction.transport import ChatTransport, HttpxChatTransport

__all__ = [
    "ChatTransport",
    "HttpxChatTransport",
    "OpenRouterExtractionAdapter",
    "build_openrouter_extractor",
]
