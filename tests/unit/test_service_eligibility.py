"""Door delivery is a capability a rate must declare, never a default.

Found in a live smoke test: an enquiry stating "Delivery: Door Delivery"
validated, priced, and sailed through to a sent quotation on a rate that only
covers airport-to-airport — because eligibility never looked at
``delivery_type`` and no rate carried a field to check. The quotation restated
the client's door requirement above a price that did not include it.

The properties pinned here:

- a door shipment may only select a rate that says ``serves_door_delivery=True``;
- ``None`` (the provider did not say) excludes exactly as ``False`` does —
  an undeclared capability is not an offered one;
- when nothing qualifies, the outcome is the existing ``NO_ELIGIBLE_RATE`` and
  nothing is sent — no email, no approval packet, no gate;
- a non-door shipment is untouched by any of this;
- the approval gate still stands behind every selectable rate;
- and the composer refuses outright to build a door quotation on a non-door
  rate, so even a future bug upstream cannot produce the bad email again.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from tests.unit.test_gmail_thread import ScriptedExtractor, StubSource

from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.routing import StatedLocationResolver
from translog_quote.adapters.webcargo import DemoRateProvider, MockWebCargoAdapter
from translog_quote.config import Settings
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.quotation import (
    Approved,
    IncompatibleService,
    ReviewPacket,
    build_quotation,
)
from translog_quote.domain.rates import (
    ExclusionReason,
    Rate,
    RateRestrictions,
    Selection,
    TransitTime,
    TransitUnit,
    filter_rates,
)
from translog_quote.domain.shipment import (
    CargoDimensions,
    DeliveryType,
    RequestSource,
    ShipmentRecord,
)
from translog_quote.domain.validation import validate_shipment
from translog_quote.domain.workflow import RequestState
from translog_quote.interface.web.live_session import LiveSession
from translog_quote.pipeline import RateSearchStage

WHEN = date(2026, 9, 2)
RESOLVER = StatedLocationResolver()


def rate(code: str, *, door: bool | None) -> Rate:
    return Rate(
        carrier_code=code,
        carrier_name=f"{code} Airways",
        product="GEN",
        total_amount=Decimal("100.00"),
        currency="INR",
        transit=TransitTime(value=2, unit=TransitUnit.DAYS),
        restrictions=RateRestrictions(serves_door_delivery=door),
    )


def record(delivery: DeliveryType, *, address: str | None = None) -> ShipmentRecord:
    return ShipmentRecord(
        request_id="R-1",
        source=RequestSource.EMAIL,
        origin="Mumbai",
        destination="Dubai",
        weight_kg=500.0,
        dimensions_in=CargoDimensions(length=34, width=24, height=6),
        commodity="Engineering components",
        cargo_type="Non-Haz",
        is_chemical=False,
        pcs=10,
        delivery_type=delivery,
        delivery_address=address
        or ("Warehouse 4, Dubai" if delivery is DeliveryType.DOOR else None),
    )


# --- the filter rule ------------------------------------------------------------


def test_a_door_request_keeps_a_door_capable_rate() -> None:
    outcome = filter_rates((rate("EK", door=True),), requires_door_delivery=True)

    assert [r.carrier_code for r in outcome.eligible] == ["EK"]
    assert outcome.excluded == ()


def test_a_door_request_excludes_an_airport_only_rate() -> None:
    outcome = filter_rates((rate("TK", door=False),), requires_door_delivery=True)

    assert outcome.eligible == ()
    assert outcome.excluded[0].reason is ExclusionReason.SERVICE_NOT_AVAILABLE
    assert "does not offer door delivery" in outcome.excluded[0].detail


def test_an_undeclared_capability_never_satisfies_a_door_request() -> None:
    """None is "did not say", and "did not say" is not an offer."""
    outcome = filter_rates((rate("UL", door=None),), requires_door_delivery=True)

    assert outcome.eligible == ()
    assert outcome.excluded[0].reason is ExclusionReason.SERVICE_NOT_AVAILABLE
    assert "undeclared" in outcome.excluded[0].detail


def test_a_non_door_request_ignores_the_capability_entirely() -> None:
    """Airport-to-airport shipments see exactly the old behaviour."""
    rates = (rate("TK", door=False), rate("UL", door=None), rate("EK", door=True))

    outcome = filter_rates(rates, requires_door_delivery=False)

    assert [r.carrier_code for r in outcome.eligible] == ["TK", "UL", "EK"]


# --- the stage reads the record's own delivery type -----------------------------


def test_a_door_shipment_selects_only_a_door_capable_carrier() -> None:
    """Against the demo provider: TK is fastest but airport-only; EK wins."""
    outcome = RateSearchStage(provider=DemoRateProvider(), resolver=RESOLVER).run(
        "R-1", record(DeliveryType.DOOR), on_date=WHEN
    )

    assert outcome.state is RequestState.RATE_SELECTED
    assert outcome.selection is not None
    assert outcome.selection.rate.carrier_code == "EK"
    assert outcome.selection.rate.restrictions.serves_door_delivery is True
    excluded = {e.rate.carrier_code: e.reason for e in outcome.filtered.excluded}
    assert excluded["TK"] is ExclusionReason.SERVICE_NOT_AVAILABLE


def test_an_airport_shipment_still_selects_the_fastest_rate() -> None:
    outcome = RateSearchStage(provider=DemoRateProvider(), resolver=RESOLVER).run(
        "R-1", record(DeliveryType.AIRPORT), on_date=WHEN
    )

    assert outcome.selection is not None
    assert outcome.selection.rate.carrier_code == "TK"


def test_no_door_capable_rate_means_no_eligible_rate() -> None:
    """The existing terminal outcome, not a wrong selection and not a crash.

    The mock adapter's fixture rates declare no door capability at all, so a
    door shipment against it must find nothing.
    """
    outcome = RateSearchStage(provider=MockWebCargoAdapter(), resolver=RESOLVER).run(
        "R-1", record(DeliveryType.DOOR), on_date=WHEN
    )

    assert outcome.state is RequestState.NO_ELIGIBLE_RATE
    assert outcome.selection is None


# --- the composer refuses to lie ------------------------------------------------


def _packet(delivery: DeliveryType, *, door: bool | None) -> ReviewPacket:
    rec = record(delivery)
    chosen = rate("XX", door=door)
    return ReviewPacket(
        request_id="R-1",
        record=rec,
        validation=validate_shipment(rec),
        clarification_sent=False,
        rates=filter_rates((chosen,)),
        selection=Selection(rate=chosen, reason="test"),
    )


def test_the_composer_refuses_a_door_quotation_on_a_non_door_rate() -> None:
    approved = Approved(by="A. Operator", at=datetime(2026, 9, 2, tzinfo=UTC))

    for door in (False, None):
        with pytest.raises(IncompatibleService):
            build_quotation(_packet(DeliveryType.DOOR, door=door), approved, is_simulated=True)


def test_the_quotation_states_its_delivery_scope() -> None:
    approved = Approved(by="A. Operator", at=datetime(2026, 9, 2, tzinfo=UTC))

    door = build_quotation(_packet(DeliveryType.DOOR, door=True), approved, is_simulated=True)
    airport = build_quotation(_packet(DeliveryType.AIRPORT, door=None), approved, is_simulated=True)

    assert "Door delivery included" in door.body_text
    assert "Airport to airport" in airport.body_text
    assert "Door delivery included" not in airport.body_text


# --- end to end: gate intact, nothing sent on a dead end ------------------------


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


def _door_extraction() -> ExtractionResult:
    return ExtractionResult(
        origin=ExtractedValue[str].stated("Mumbai"),
        destination=ExtractedValue[str].stated("Dubai"),
        weight_kg=ExtractedValue[float].stated(500.0),
        dimensions_in=ExtractedValue[CargoDimensions].stated(
            CargoDimensions(length=34, width=24, height=6)
        ),
        commodity=ExtractedValue[str].stated("Engineering components"),
        cargo_type=ExtractedValue[str].stated("Non-Haz"),
        is_chemical=ExtractedValue[bool].stated(value=False),
        pcs=ExtractedValue[int].stated(10),
        delivery_type=ExtractedValue[DeliveryType].stated(DeliveryType.DOOR),
        delivery_address=ExtractedValue[str].stated("Warehouse 4, Dubai"),
    )


def _enquiry() -> RawEmail:
    return RawEmail(
        message_id="<door-1@mail.example.com>",
        from_address="client@example.com",
        subject="Rate required - Mumbai to Dubai - door delivery",
        body_text="500 KG, Mumbai to Dubai, door delivery to Warehouse 4.",
        received_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
    )


def test_a_door_enquiry_halts_at_the_gate_with_a_door_capable_rate(
    settings: Settings,
) -> None:
    """The live path: door enquiry -> door-capable selection -> gate. Unapproved,
    nothing is sent; and the decision is what sends, exactly as before."""
    sink = CollectingEmailSink()
    session = LiveSession(
        settings,
        source=StubSource(_enquiry()),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(_door_extraction()),  # type: ignore[arg-type]
        resolver=StatedLocationResolver(),
    )

    session.poll()
    request = next(iter(session.requests.values()))

    assert request.state is RequestState.RATE_SELECTED
    assert request.rates is not None
    assert request.rates.selection is not None
    assert request.rates.selection.rate.restrictions.serves_door_delivery is True
    assert request.awaiting_quotation_decision is True
    assert sink.sent == [], "nothing sends without a decision"

    decided = session.decide(request.request_id, choice="approve", by="A. Operator")

    assert decided.state is RequestState.QUOTATION_SENT
    client_mail = [m for m in sink.sent if m.to_address == "client@example.com"]
    assert len(client_mail) == 1
    assert "Door delivery included" in client_mail[0].body_text


def test_a_dead_end_sends_nothing_and_offers_no_approval(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No door-capable rate -> NO_ELIGIBLE_RATE. No email, no gate, and no
    automatic clarification either — a thin market is not missing information."""
    sink = CollectingEmailSink()
    session = LiveSession(
        settings,
        source=StubSource(_enquiry()),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(_door_extraction()),  # type: ignore[arg-type]
        resolver=StatedLocationResolver(),
    )
    # The demo provider does have door-capable carriers, so remove them from
    # reach by driving the stage with the mock adapter's fixture rates instead.
    from translog_quote import bootstrap

    monkeypatch.setattr(bootstrap, "build_demo_rate_provider", MockWebCargoAdapter)
    session.poll()

    request = next(iter(session.requests.values()))

    assert request.state is RequestState.NO_ELIGIBLE_RATE
    assert request.packet is None
    assert request.awaiting_quotation_decision is False
    assert request.clarification is None
    assert sink.sent == []
