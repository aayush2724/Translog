"""Any origin, any destination — and still never a guessed airport.

The product requirement these pin: a quotation enquiry must not be turned away
because the place it names is not in a list this repository ships. There is no
such list any more. What replaced it is a resolver port, and the properties that
matter are the ones a table could not give us:

- an unfamiliar place progresses all the way to the human approval gate;
- no code is ever attached to a place unless something resolved it, and
  whatever resolved it is named — so a guess has nowhere to hide;
- the production resolver refuses rather than falling back to the demo one;
- a refusal is one request's problem, never the batch's;
- the approval gates are exactly where they were.

The cases are lettered to match the acceptance list they were written against.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.test_gmail_thread import ScriptedExtractor, StubSource

from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.routing import StatedLocationResolver, WebCargoLocationResolver
from translog_quote.adapters.webcargo import DemoRateProvider
from translog_quote.config import Settings, WebCargoMode
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.rates import LocationRef
from translog_quote.domain.shipment import (
    CargoDimensions,
    DeliveryType,
    RequestSource,
    ShipmentRecord,
)
from translog_quote.domain.workflow import RequestState
from translog_quote.errors import UnresolvedLocation
from translog_quote.interface.web.live_session import LiveSession
from translog_quote.pipeline import RateSearchStage
from translog_quote.pipeline.rate_search import build_query

WHEN = datetime(2026, 9, 2, tzinfo=UTC).date()
RESOLVER = StatedLocationResolver()

#: Lanes that the deleted table could not express. Every one of these was a
#: refused enquiry before this change; C and B are the ones the client reported.
WORLDWIDE = [
    ("Mumbai", "Dubai"),  # A — was in the table
    ("Hyderabad", "Amsterdam"),  # B — was not
    ("Tokyo", "Frankfurt"),  # C — neither end was
    ("Dubai, UAE", "Singapore"),  # E — a normal geographic variant
    ("Dubai, United Arab Emirates", "São Paulo"),
    ("Reykjavík", "Ulaanbaatar"),
]


@pytest.fixture
def settings() -> Settings:
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    return base.model_copy(
        update={
            "openrouter": base.openrouter.model_copy(update={"api_key": "test-not-a-credential"}),
            "demo": base.demo.model_copy(update={"state_dir": Path(tempfile.mkdtemp())}),
            "gmail": base.gmail.model_copy(
                update={
                    "test_address": "translog@example.com",
                    "sender_address": "translog@example.com",
                    "approver_address": "approvals@translog.example",
                    "send_enabled": True,
                }
            ),
        }
    )


def _complete(origin: str, destination: str) -> ExtractionResult:
    return ExtractionResult(
        origin=ExtractedValue[str].stated(origin),
        destination=ExtractedValue[str].stated(destination),
        weight_kg=ExtractedValue[float].stated(500.0),
        dimensions_in=ExtractedValue[CargoDimensions].stated(
            CargoDimensions(length=34, width=24, height=6)
        ),
        commodity=ExtractedValue[str].stated("Engineering components"),
        cargo_type=ExtractedValue[str].stated("Non-Haz"),
        is_chemical=ExtractedValue[bool].stated(value=False),
        pcs=ExtractedValue[int].stated(10),
        delivery_type=ExtractedValue[DeliveryType].stated(DeliveryType.AIRPORT),
    )


def _record(origin: str, destination: str) -> ShipmentRecord:
    """A complete, validated-shape record for the given lane."""
    return ShipmentRecord(
        request_id="R-1",
        source=RequestSource.EMAIL,
        origin=origin,
        destination=destination,
        weight_kg=500.0,
        dimensions_in=CargoDimensions(length=34, width=24, height=6),
        commodity="Engineering components",
        cargo_type="Non-Haz",
        is_chemical=False,
        pcs=10,
        delivery_type=DeliveryType.AIRPORT,
    )


def _email(mid: str, subject: str, minutes: int, sender: str = "client@example.com") -> RawEmail:
    return RawEmail(
        message_id=mid,
        from_address=sender,
        subject=subject,
        body_text="See details.",
        received_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC) + timedelta(minutes=minutes),
    )


# --- A, B, C, E: any lane reaches a priced, selectable result -------------------


@pytest.mark.parametrize(("origin", "destination"), WORLDWIDE)
def test_any_lane_worldwide_is_accepted_and_priced(origin: str, destination: str) -> None:
    outcome = RateSearchStage(provider=DemoRateProvider(), resolver=RESOLVER).run(
        "R-1", _record(origin, destination), on_date=WHEN
    )

    assert outcome.state is RequestState.RATE_SELECTED
    assert outcome.selection is not None


@pytest.mark.parametrize(("origin", "destination"), WORLDWIDE)
def test_no_airport_is_ever_guessed(origin: str, destination: str) -> None:
    """H. The demo resolves nothing, so it must attach nothing."""
    query = build_query(_record(origin, destination), on_date=WHEN, resolver=RESOLVER)

    for end in (query.origin, query.destination):
        assert end.code is None
        assert end.resolved_by is None
    assert query.origin.stated == origin
    assert query.destination.stated == destination


def test_a_code_cannot_exist_without_naming_what_produced_it() -> None:
    """H, structurally. The type refuses an unattributed code."""
    with pytest.raises(ValueError, match="resolver"):
        LocationRef(stated="Mumbai", code="BOM")

    attributed = LocationRef(stated="Mumbai", code="BOM", resolved_by="webcargo")
    assert attributed.display == "BOM"


def test_a_geographic_variant_is_carried_through_verbatim() -> None:
    """E. "Dubai, UAE" is neither rewritten nor truncated to "Dubai"."""
    query = build_query(_record("Dubai, UAE", "Singapore"), on_date=WHEN, resolver=RESOLVER)

    assert query.origin.stated == "Dubai, UAE"
    assert query.origin.display == "Dubai, UAE"


# --- D, I: two clients, same lane, independent, nothing fabricated --------------


def test_two_clients_on_the_same_lane_are_quoted_independently() -> None:
    """D. No shared lane state exists, so the two cannot interfere."""
    first = RateSearchStage(provider=DemoRateProvider(), resolver=RESOLVER).run(
        "R-CLIENT-A", _record("Hyderabad", "Amsterdam"), on_date=WHEN
    )
    second = RateSearchStage(provider=DemoRateProvider(), resolver=RESOLVER).run(
        "R-CLIENT-B", _record("Hyderabad", "Amsterdam"), on_date=WHEN
    )

    assert first.request_id != second.request_id
    assert first.state is second.state is RequestState.RATE_SELECTED
    assert first.selection is not None
    assert second.selection is not None


@pytest.mark.parametrize(("origin", "destination"), WORLDWIDE)
def test_no_rate_is_fabricated_for_an_unfamiliar_lane(origin: str, destination: str) -> None:
    """I. Rates come from the simulated provider and are labelled as simulated.

    Accepting any lane must not quietly turn invented figures into apparent
    provider data — the disclosure is what makes the demo honest.
    """
    outcome = RateSearchStage(provider=DemoRateProvider(), resolver=RESOLVER).run(
        "R-1", _record(origin, destination), on_date=WHEN
    )

    assert outcome.uses_mock_data is True


# --- F: a genuine refusal, and no fallback --------------------------------------


def test_the_production_resolver_refuses_rather_than_resolving() -> None:
    """F. And it names no code of any kind in the refusal."""
    with pytest.raises(UnresolvedLocation) as raised:
        WebCargoLocationResolver().resolve("Dubai")

    assert "not implemented" in str(raised.value)


def test_the_production_resolver_never_falls_back_to_the_demo_one() -> None:
    """The hard requirement: real must not degrade into stated.

    Checked by behaviour rather than by reading the source — every place the
    demo resolver would happily accept must still refuse here.
    """
    real = WebCargoLocationResolver()

    for origin, destination in WORLDWIDE:
        for place in (origin, destination):
            assert StatedLocationResolver().resolve(place).stated == place
            with pytest.raises(UnresolvedLocation):
                real.resolve(place)


def test_real_mode_wires_the_refusing_resolver(settings: Settings) -> None:
    """A production run must not be handed the demo resolver by the wiring."""
    from translog_quote import bootstrap

    real = settings.model_copy(
        update={"webcargo": settings.webcargo.model_copy(update={"mode": WebCargoMode.REAL})}
    )

    assert isinstance(bootstrap.build_location_resolver(real), WebCargoLocationResolver)
    assert isinstance(bootstrap.build_location_resolver(settings), StatedLocationResolver)


def test_an_empty_place_is_refused_by_the_demo_resolver() -> None:
    """The one thing the demo resolver rejects: absence, not unfamiliarity."""
    with pytest.raises(UnresolvedLocation):
        StatedLocationResolver().resolve("   ")


# --- G, J: isolation and the approval gate, end to end --------------------------


class OneBadLane:
    """A provider that cannot identify one location. The production shape."""

    resolver_id = "test-one-bad-lane"

    def resolve(self, place: str) -> LocationRef:
        if "atlantis" in place.lower():
            raise UnresolvedLocation(f"{place!r} could not be identified")
        return LocationRef(stated=place)


def test_an_unresolvable_request_does_not_block_a_valid_one(
    settings: Settings,
) -> None:
    """G. One refusal, one affected request, the rest of the poll unharmed."""
    bad = _email("<bad@x>", "Rate required - Atlantis to Tokyo", 0)
    good = _email("<good@x>", "Rate required - Hyderabad to Amsterdam", 5)
    sink = CollectingEmailSink()
    session = LiveSession(
        settings,
        source=StubSource(bad, good),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(  # type: ignore[arg-type]
            _complete("Atlantis", "Tokyo"), _complete("Hyderabad", "Amsterdam")
        ),
        resolver=OneBadLane(),
    )

    session.poll()

    by_origin = {r.record.origin: r for r in session.requests.values()}
    assert by_origin["Atlantis"].rate_failure is not None
    assert by_origin["Atlantis"].packet is None
    assert by_origin["Hyderabad"].state is RequestState.RATE_SELECTED
    assert by_origin["Hyderabad"].packet is not None
    assert sink.sent == []


def test_a_worldwide_lane_still_stops_at_the_human_approval_gate(
    settings: Settings,
) -> None:
    """J. The invariant, on a lane that could not previously be quoted at all.

    Reaching the gate is the new behaviour; stopping there is the behaviour
    that must not have changed. Nothing is sent by polling, and the quotation
    goes only after an explicit named decision.
    """
    sink = CollectingEmailSink()
    session = LiveSession(
        settings,
        source=StubSource(_email("<e@x>", "Rate required - Tokyo to Frankfurt", 0)),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(_complete("Tokyo", "Frankfurt")),  # type: ignore[arg-type]
        resolver=StatedLocationResolver(),
    )

    session.poll()
    request = next(iter(session.requests.values()))

    assert request.state is RequestState.RATE_SELECTED
    assert request.awaiting_quotation_decision is True
    assert request.decision is None
    assert sink.sent == [], "polling a worldwide lane must send nothing"

    decided = session.decide(request.request_id, choice="approve", by="A. Operator")

    assert decided.state is RequestState.QUOTATION_SENT
    assert decided.quotation_sent is True
    assert len(sink.sent) == 2, "the approver review and the client quotation"


def test_a_declined_worldwide_lane_sends_nothing_to_the_client(
    settings: Settings,
) -> None:
    """J, the other branch. A decline is a decision, and it mails no client."""
    sink = CollectingEmailSink()
    session = LiveSession(
        settings,
        source=StubSource(_email("<e@x>", "Rate required - Reykjavik to Lima", 0)),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(_complete("Reykjavík", "Lima")),  # type: ignore[arg-type]
        resolver=StatedLocationResolver(),
    )
    session.poll()
    request = next(iter(session.requests.values()))

    session.decide(request.request_id, choice="decline", by="A. Operator", reason="too slow")

    assert [m.to_address for m in sink.sent] == ["approvals@translog.example"]
