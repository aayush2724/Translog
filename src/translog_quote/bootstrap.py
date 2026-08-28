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
    from pathlib import Path

    from translog_quote.pipeline import ClarificationWorkflow
    from translog_quote.ports import (
        ClockPort,
        EmailSink,
        EmailSource,
        ExtractionPort,
        RateSearchPort,
        StorePort,
    )

__all__ = [
    "Settings",
    "authorize_gmail",
    "build_clarification_workflow",
    "build_extractor",
    "build_gmail_email_source",
    "build_rate_provider",
    "build_fixed_clock",
    "build_fixture_email_source",
    "build_memory_store",
    "build_outbox_sink",
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


def build_gmail_email_source(settings: Settings) -> EmailSource:
    """An `EmailSource` over the configured Gmail **test** mailbox (Phase 10.3).

    Receive-only, and never built implicitly: the fixture source stays the
    default everywhere, and only the explicit `gmail-test` command asks for
    this one. Refuses to build without a configured test address, and the
    transport refuses without the git-ignored OAuth token file.
    """
    from translog_quote.adapters.email import GmailEmailSource, HttpxGmailTransport
    from translog_quote.errors import PermanentFailure

    gmail = settings.gmail
    if not gmail.test_address:
        raise PermanentFailure(
            "No Gmail test mailbox configured. Set TRANSLOG_GMAIL__TEST_ADDRESS "
            "in .env (see .env.example)."
        )

    transport = HttpxGmailTransport(
        token_path=gmail.token_path,
        timeout_seconds=gmail.timeout_seconds,
        max_retries=gmail.max_retries,
        backoff_seconds=gmail.retry_backoff_seconds,
    )
    return GmailEmailSource(
        transport,
        mailbox_address=gmail.test_address,
        query=gmail.query,
        max_results=gmail.max_results,
    )


def authorize_gmail(settings: Settings) -> Path:
    """Run the one-time interactive Gmail OAuth consent; returns the token path.

    Only ever called by the explicit `gmail-auth` command — nothing authorizes
    automatically.
    """
    from translog_quote.adapters.email.gmail_auth import run_consent_flow

    return run_consent_flow(
        client_secret_path=settings.gmail.client_secret_path,
        token_path=settings.gmail.token_path,
    )


def build_memory_store() -> StorePort:
    """The demo's request store. In memory; nothing survives the process."""
    from translog_quote.adapters.store import InMemoryStore

    return InMemoryStore()


def build_outbox_sink(directory: Path | None = None) -> EmailSink:
    """Where clarifications go. Collected in memory, and written to a directory
    when one is given. No mail is sent."""
    from translog_quote.adapters.email import CollectingEmailSink, FileOutboxSink

    return FileOutboxSink(directory) if directory is not None else CollectingEmailSink()


def build_fixed_clock(moment: object | None = None) -> ClockPort:
    """A clock that does not move, so an audit trail is diffable across runs."""
    from translog_quote.adapters.clock import FixedClock

    return FixedClock(moment)  # type: ignore[arg-type]


def build_clarification_workflow(
    settings: Settings,
    *,
    sink: EmailSink | None = None,
    store: StorePort | None = None,
    extractor: ExtractionPort | None = None,
) -> ClarificationWorkflow:
    """The clarification loop, wired to live extraction unless told otherwise.

    Every collaborator is injectable so a test can drive the real workflow with
    a stub extractor and no network.
    """
    from translog_quote.pipeline import ClarificationWorkflow

    return ClarificationWorkflow(
        extractor=extractor or build_extractor(settings),
        sink=sink or build_outbox_sink(),
        store=store or build_memory_store(),
        clock=build_fixed_clock(),
    )


def build_rate_provider(settings: Settings) -> RateSearchPort:
    """The configured rate provider.

    `mock` yields fixture rates and labels them as such; `real` yields an adapter
    that refuses with the reason, because no WebCargo API contract has been
    provided. This is the only place either concrete class is named.
    """
    from translog_quote.config import WebCargoMode

    if settings.webcargo.mode is WebCargoMode.REAL:
        from translog_quote.adapters.webcargo import RealWebCargoAdapter

        return RealWebCargoAdapter(base_url=settings.webcargo.base_url)

    from translog_quote.adapters.webcargo import MockWebCargoAdapter

    return MockWebCargoAdapter()
