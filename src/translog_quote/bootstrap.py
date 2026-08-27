"""Composition root — the only module permitted to name a concrete adapter.

Read this file to learn how the system is assembled. That it is the single
exception to the dependency rule is what makes the rest of the rule enforceable:
without it, "swap the adapter" degrades into a flag check somewhere in the middle
of the pipeline.

Every function here returns a **port or a domain type**, never an adapter type.
That is what lets `interface/` drive the real system while remaining unable to
name — or accidentally depend on — the implementation behind it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.config import Settings, load_settings

if TYPE_CHECKING:
    from translog_quote.ports import EmailSource, ExtractionPort

__all__ = [
    "Settings",
    "build_extractor",
    "build_fixture_email_source",
    "load_settings",
]


def build_extractor(settings: Settings) -> ExtractionPort:
    """The live extraction port, backed by OpenRouter.

    Raises `PermanentFailure` at construction when the API key or model is
    missing, rather than at first use — a missing key found halfway through a
    client's email is a worse place to discover it.
    """
    from translog_quote.adapters.extraction import build_openrouter_extractor

    return build_openrouter_extractor(settings)


def build_fixture_email_source(settings: Settings, scenario: str) -> EmailSource:
    """An `EmailSource` over one fixture scenario directory.

    Returns the port, not the fixture-scenario object: callers get `RawEmail`
    values and nothing that would tie them to how those emails were stored.
    """
    from translog_quote.adapters.email import FixtureEmailSource

    return FixtureEmailSource(settings.demo.email_fixtures_dir / scenario)
