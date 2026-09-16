"""Worker-side location resolution: the browser adapter resolves the stated
place to a code immediately before searching, without touching the queued
payload, and refuses (never guesses) an unresolvable one — no search then.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pytest

from translog_quote.adapters.routing import CanonicalLocationResolver
from translog_quote.adapters.webcargo.browser.adapter import WebCargoBrowserAdapter
from translog_quote.domain.rates import LocationRef, RateQuery
from translog_quote.domain.shipment import CargoDimensions
from translog_quote.errors import UnresolvedLocation
from translog_quote.interface.jobs import RateSearchJobRequest


class _FakeManager:
    """Records whether a browser page was ever opened."""

    def __init__(self) -> None:
        self.opened = 0

    @contextmanager
    def job_page(self):  # type: ignore[no-untyped-def]
        self.opened += 1
        yield object()


def _query(origin: str, destination: str) -> RateQuery:
    return RateQuery(
        origin=LocationRef(stated=origin),
        destination=LocationRef(stated=destination),
        weight_kg=500.0,
        dimensions_in=CargoDimensions(length=40, width=30, height=25),
        pieces=1,
        date=date(2026, 9, 25),
        commodity="General Cargo",
    )


def _adapter(manager: _FakeManager) -> WebCargoBrowserAdapter:
    return WebCargoBrowserAdapter(
        manager=manager,
        base_url="https://example.invalid",
        wrap_page=lambda page: page,
        search_timeout_seconds=5,
        navigation_timeout_seconds=5,
        resolver=CanonicalLocationResolver(),
    )


@pytest.mark.parametrize(
    ("origin", "destination", "origin_code", "destination_code"),
    [
        ("Delhi, India", "Singapore", "DEL", "SIN"),
        ("Bangalore", "Mumbai, India", "BLR", "BOM"),
        ("DEL", "SIN", "DEL", "SIN"),  # explicit codes pass through
    ],
)
def test_stated_places_reach_the_adapter_as_codes(
    origin: str, destination: str, origin_code: str, destination_code: str
) -> None:
    resolved = _adapter(_FakeManager())._resolved(_query(origin, destination))
    # `.display` is exactly what `_fill_location` types into WebCargo.
    assert resolved.origin.display == origin_code
    assert resolved.destination.display == destination_code
    # The client's original wording is preserved on the query.
    assert resolved.origin.stated == origin
    assert resolved.destination.stated == destination


def test_unresolved_location_refuses_and_starts_no_search() -> None:
    manager = _FakeManager()
    with pytest.raises(UnresolvedLocation):
        _adapter(manager).search(_query("some village nobody tabulated", "Singapore"))
    assert manager.opened == 0  # no page opened => no WebCargo search attempted


def test_queued_job_payload_keeps_the_clients_stated_locations() -> None:
    # Resolution happens at worker execution, never at enqueue: the payload and
    # the query it builds still carry the original wording, with no code.
    request = RateSearchJobRequest(
        origin="Delhi, India",
        destination="Singapore",
        weight_kg=500.0,
        dimensions_in=CargoDimensions(length=40, width=30, height=25),
        pieces=1,
        search_date=date(2026, 9, 25),
        commodity="General Cargo",
        goods_type="0000 - General Cargo",
    )
    assert request.origin == "Delhi, India"
    assert request.destination == "Singapore"
    query = request.to_query()
    assert query.origin.stated == "Delhi, India"
    assert query.origin.code is None
