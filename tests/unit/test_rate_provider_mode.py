"""The rate-provider switch, which is the whole integration boundary.

`build_rate_provider` is the only place in the system that decides which
provider a rate comes from. What matters is that the decision is *explicit*:
the demo must keep working with nothing configured, and no provider that
reaches out of the process may ever activate by accident.

The production (browser) provider is wired by the browser worker's own
composition path when its mode is selected; these tests pin the simulated
modes and the default.
"""

from __future__ import annotations

import pytest
from tests.unit.test_rate_search import WHEN, record

from translog_quote import bootstrap
from translog_quote.adapters.routing import StatedLocationResolver
from translog_quote.config import Settings, WebCargoMode
from translog_quote.pipeline import build_query

RESOLVER = StatedLocationResolver()


def settings_with(mode: WebCargoMode | None = None) -> Settings:
    """Settings with nothing but the mode set — no credentials anywhere."""
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    if mode is None:
        return base
    return base.model_copy(update={"webcargo": base.webcargo.model_copy(update={"mode": mode})})


# --- the default is mock, and needs no configuration ----------------------------


def test_nothing_configured_yields_the_mock_provider() -> None:
    """The demo must run on a clean checkout with no .env and no credentials."""
    provider = bootstrap.build_rate_provider(settings_with())

    assert provider.adapter_id.startswith("mock")


def test_the_mock_provider_needs_no_credentials_to_search() -> None:
    settings = settings_with()
    assert settings.webcargo.username is None
    assert settings.webcargo.password is None

    result = bootstrap.build_rate_provider(settings).search(
        build_query(record(), on_date=WHEN, resolver=RESOLVER)
    )

    assert result.rates
    assert result.adapter_id.startswith("mock")


def test_mock_mode_stated_explicitly_is_still_mock() -> None:
    provider = bootstrap.build_rate_provider(settings_with(WebCargoMode.MOCK))

    assert provider.adapter_id.startswith("mock")


# --- every simulated provider declares itself simulated --------------------------


def test_demo_mode_is_opt_in_and_flagged_simulated() -> None:
    """The switch is the only route to the demo provider, and nothing it
    returns can be presented as live provider data."""
    provider = bootstrap.build_rate_provider(settings_with(WebCargoMode.DEMO))

    result = provider.search(build_query(record(), on_date=WHEN, resolver=RESOLVER))

    assert result.adapter_id == "demo-webcargo"
    assert result.is_simulated is True


def test_browser_mode_refuses_outside_the_browser_worker() -> None:
    """Only the worker owns the persistent session. Any other process asked
    for browser mode is told to enqueue a job, not handed a browser."""
    from translog_quote.errors import PermanentFailure

    with pytest.raises(PermanentFailure, match="browser worker"):
        bootstrap.build_rate_provider(settings_with(WebCargoMode.BROWSER))


def test_the_mode_is_controlled_by_the_documented_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TRANSLOG_WEBCARGO__MODE` is the documented control, and unsetting it
    returns to the mock default rather than to anything that calls out."""
    monkeypatch.setenv("TRANSLOG_WEBCARGO__MODE", "demo")
    assert Settings().webcargo.mode is WebCargoMode.DEMO

    monkeypatch.delenv("TRANSLOG_WEBCARGO__MODE")
    assert Settings(_env_file=None).webcargo.mode is WebCargoMode.MOCK  # type: ignore[call-arg]
