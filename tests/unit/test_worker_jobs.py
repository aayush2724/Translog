"""The worker-side job function: the domain decides, the provider supplies.

Driven with the mock adapter — no queue, no browser. What these pin:

- search -> filter -> select runs in the worker's application layer;
- the serialized result keeps the full accountability trail;
- browser mode without a wired extraction layer refuses loudly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.unit.test_rate_search_jobs import request

from translog_quote.adapters.webcargo import MockWebCargoAdapter
from translog_quote.errors import PermanentFailure
from translog_quote.interface.worker import jobs as worker_jobs


@pytest.fixture(autouse=True)
def _reset_provider() -> object:
    yield None
    worker_jobs.set_provider(None)


def payload(**overrides: object) -> dict[str, object]:
    return request(**overrides).model_dump(mode="json")  # type: ignore[arg-type]


def test_a_job_searches_filters_selects_and_serialises() -> None:
    worker_jobs.set_provider(MockWebCargoAdapter())

    out = worker_jobs.run_rate_search(payload())

    assert out["adapter_id"] == "mock-webcargo"
    assert out["is_simulated"] is True  # the mock declares itself simulated
    assert out["returned"] == 6

    # The fastest eligible rate wins: TK at 1 day (no liquids restriction
    # applies when the cargo's physical form is unknown).
    selection = out["selection"]
    assert isinstance(selection, dict)
    assert selection["rate"]["carrier_code"] == "TK"

    # The exclusions travel with their reasons — the accountability trail.
    reasons = {e["rate"]["carrier_code"]: e["reason"] for e in out["filtered"]["excluded"]}
    assert reasons["HY"] == "incomplete_rate"
    assert reasons["UL"] == "unrankable_no_transit"


def test_liquid_cargo_moves_the_decision_to_the_filters_not_the_adapter() -> None:
    """Same provider, different eligibility: TK (fastest AND cheapest) falls
    to the liquids rule and EK wins on transit — proof the adapter did not
    decide anything."""
    worker_jobs.set_provider(MockWebCargoAdapter())

    out = worker_jobs.run_rate_search(payload(cargo_is_liquid=True))

    selection = out["selection"]
    assert isinstance(selection, dict)
    assert selection["rate"]["carrier_code"] == "EK"
    reasons = {e["rate"]["carrier_code"]: e["reason"] for e in out["filtered"]["excluded"]}
    assert reasons["TK"] == "carrier_restricted"


def test_the_result_echoes_the_query_it_answered() -> None:
    worker_jobs.set_provider(MockWebCargoAdapter())

    out = worker_jobs.run_rate_search(payload())

    query = out["query"]
    assert isinstance(query, dict)
    assert query["origin"]["stated"] == "Bangalore"
    assert query["destination"]["stated"] == "Manila"


def test_a_malformed_payload_is_rejected_before_any_search() -> None:
    calls: list[object] = []

    class CountingProvider:
        adapter_id = "counting"

        def search(self, query: object) -> object:
            calls.append(query)
            raise AssertionError("search must not be reached")

    worker_jobs.set_provider(CountingProvider())  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        worker_jobs.run_rate_search({"origin": "", "destination": "Manila"})

    assert calls == []  # validation is the edge; nothing leaked past it


def test_browser_mode_without_a_configured_url_refuses_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No endpoint is written in this repository: browser mode without a
    configured WebCargo URL refuses with the reason — and certainly never
    falls back to simulated rates."""
    worker_jobs.set_provider(None)
    monkeypatch.setenv("TRANSLOG_WEBCARGO__MODE", "browser")
    monkeypatch.setenv("TRANSLOG_WEBCARGO__BASE_URL", "")

    with pytest.raises(PermanentFailure, match="No WebCargo URL configured"):
        worker_jobs.run_rate_search(payload())
