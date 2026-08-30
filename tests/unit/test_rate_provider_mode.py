"""The mock/real rate-provider switch, which is the whole integration boundary.

`build_rate_provider` is the only place in the system that decides whether a
rate comes from fixture data or from a provider. Until the Freightos/WebCargo
partner contract exists, what matters is that the decision is *explicit*: the
demo must keep working with nothing configured, and real mode must never
activate by accident.

These tests exist so that the boundary stays ready. When the official contract
arrives, the real branch gains an implementation and every assertion here still
describes behaviour it must keep.
"""

from __future__ import annotations

import pytest
from tests.unit.test_rate_search import WHEN, record

from translog_quote import bootstrap
from translog_quote.adapters.routing import StatedLocationResolver
from translog_quote.config import Settings, WebCargoMode
from translog_quote.errors import PermanentFailure
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


# --- real mode is opt-in, and currently refuses ---------------------------------


def test_real_mode_must_be_asked_for_by_configuration() -> None:
    """The switch is the only route to the real adapter. Nothing else — no
    credential being present, no environment, no fallback — selects it."""
    provider = bootstrap.build_rate_provider(settings_with(WebCargoMode.REAL))

    assert not provider.adapter_id.startswith("mock")


def test_real_mode_refuses_at_search_rather_than_inventing_rates() -> None:
    """Until the partner contract exists, the honest outcome is a refusal that
    names the reason — never a plausible-looking rate."""
    provider = bootstrap.build_rate_provider(settings_with(WebCargoMode.REAL))

    with pytest.raises(PermanentFailure, match="not implemented"):
        provider.search(build_query(record(), on_date=WHEN, resolver=RESOLVER))


def test_the_mode_is_controlled_by_the_documented_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TRANSLOG_WEBCARGO__MODE` is the documented control, and unsetting it
    returns to the mock default rather than to anything that calls out."""
    monkeypatch.setenv("TRANSLOG_WEBCARGO__MODE", "real")
    assert Settings().webcargo.mode is WebCargoMode.REAL

    monkeypatch.delenv("TRANSLOG_WEBCARGO__MODE")
    assert Settings(_env_file=None).webcargo.mode is WebCargoMode.MOCK  # type: ignore[call-arg]


def test_the_refusal_names_no_endpoint_and_no_credential() -> None:
    """The error is read by whoever tries to switch modes early. It must say
    what is missing without publishing a guessed endpoint."""
    provider = bootstrap.build_rate_provider(settings_with(WebCargoMode.REAL))

    with pytest.raises(PermanentFailure) as excinfo:
        provider.search(build_query(record(), on_date=WHEN, resolver=RESOLVER))

    message = str(excinfo.value).lower()
    for forbidden in ("http://", "https://", "webcargonet", "password", "cookie"):
        assert forbidden not in message
