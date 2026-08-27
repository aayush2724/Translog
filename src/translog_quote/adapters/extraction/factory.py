"""Wiring the extraction adapter from configuration.

Kept apart from the adapter so that the adapter itself takes a transport and a
model string and nothing else — which is what lets it be tested with no
settings object, no environment and no network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.adapters.extraction.openrouter import OpenRouterExtractionAdapter
from translog_quote.adapters.extraction.transport import HttpxChatTransport
from translog_quote.errors import PermanentFailure

if TYPE_CHECKING:
    from translog_quote.config import Settings


def build_openrouter_extractor(settings: Settings) -> OpenRouterExtractionAdapter:
    """Build the live adapter, or fail with a message that says what is missing.

    Fails at construction rather than on first use: a missing key discovered
    while a client's email is halfway through the pipeline is a worse place to
    find out than at startup.
    """
    openrouter = settings.openrouter

    if openrouter.api_key is None:
        raise PermanentFailure(
            "No OpenRouter API key configured. Set TRANSLOG_OPENROUTER__API_KEY "
            "in the environment or in .env (see .env.example)."
        )

    if not openrouter.model:
        raise PermanentFailure("No extraction model configured. Set TRANSLOG_OPENROUTER__MODEL.")

    transport = HttpxChatTransport(
        api_key=openrouter.api_key.get_secret_value(),
        base_url=openrouter.base_url,
        timeout_seconds=openrouter.timeout_seconds,
        max_retries=openrouter.max_retries,
        backoff_seconds=openrouter.retry_backoff_seconds,
    )
    return OpenRouterExtractionAdapter(transport=transport, model=openrouter.model)
