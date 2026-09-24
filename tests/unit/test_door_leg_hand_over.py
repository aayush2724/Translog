"""Rates that came back but are all excluded go to a person, never to the client.

Before: any search with no eligible rate emailed the client "we are unable to
source a rate" and closed the request. For a door-delivery request that fired
every time — WebCargo's results do not state door capability, so every
airport-to-airport rate was excluded as "door delivery not confirmed" — and the
same auto-email would follow any filter or mapping gap on rates that did exist.

Now (business decision, option B):
- rows returned, every one excluded only for an unconfirmed door leg ->
  MANUAL_REVIEW with "Port/airport rates available, door leg needs manual
  pricing.", the rates kept for the operator, nothing sent to the client, and
  nothing quotable;
- rows returned, all excluded for any other reason -> MANUAL_REVIEW with the
  reason in plain words, nothing sent;
- zero rows -> the existing automatic "no rates" notice, unchanged.

Driven through the production browser-mode bridge (`LiveSession` polling a
completed worker job); only the mailbox, model, sink and queue are stubbed.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from tests.unit.test_end_to_end_regressions import (
    CLIENT,
    TODAY,
    _browser_session,
    _email,
    _extraction,
    _full_record,
)
from tests.unit.test_gmail_thread import ScriptedExtractor, StubSource
from tests.unit.test_live_browser_bridge import (
    JOB_ID,
    QueueSpy,
    _forbid_demo_provider,
    _install_queue,
    _settings,
)
from tests.unit.test_reply_gate_and_extraction_isolation import _ops_browser

from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.store import InMemoryStore
from translog_quote.config import WebCargoMode
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.rates import (
    FASTEST_ELIGIBLE,
    ExclusionReason,
    Rate,
    RateQuery,
    RateRestrictions,
    TransitTime,
    TransitUnit,
    filter_rates,
    select_rate,
)
from translog_quote.domain.rates import LocationRef as RateLocationRef
from translog_quote.domain.shipment import CargoDimensions, DeliveryType
from translog_quote.domain.workflow import RequestState
from translog_quote.interface.jobs import JobState, JobStatus, RateSearchJobResult
from translog_quote.interface.web import live_serialize
from translog_quote.interface.web.live_serialize import rates_json
from translog_quote.interface.web.live_session import (
    DOOR_LEG_NOTE,
    LiveSequenceError,
    LiveSession,
)
from translog_quote.pipeline.audit import AuditEventType

SHIP = ExtractedValue[date].stated(date(2026, 10, 5))


def _door_extraction() -> ExtractionResult:
    return _extraction(
        ship_date=SHIP,
        delivery_type=ExtractedValue[DeliveryType].stated(DeliveryType.DOOR),
        delivery_address=ExtractedValue[str].stated("Warehouse 4, Jurong, Singapore"),
    )


def _airport_extraction() -> ExtractionResult:
    return _extraction(ship_date=SHIP)


def _rate(code: str, name: str, amount: str, *, door: bool | None = None) -> Rate:
    """An airport-to-airport WebCargo row as the browser mapper produces it:
    complete, rankable, and — by default — silent about door delivery."""
    return Rate(
        carrier_code=code,
        carrier_name=name,
        product="GEN",
        total_amount=Decimal(amount),
        currency="INR",
        transit=TransitTime(value=20, unit=TransitUnit.HOURS),
        restrictions=RateRestrictions(serves_door_delivery=door),
        departure_date_label="Mon 05 Oct",
    )


AIRPORT_RATES = (
    _rate("TK", "Turkish Cargo", "16900.00"),
    _rate("EK", "Emirates", "20762.00"),
)


def _completed(rates: tuple[Rate, ...], *, door: bool) -> JobStatus:
    """The worker's COMPLETED result, filtered and selected by the same domain
    code the worker runs."""
    filtered = filter_rates(rates, requires_door_delivery=door)
    result = RateSearchJobResult(
        adapter_id="webcargo-browser",
        is_simulated=False,
        returned=len(rates),
        query=RateQuery(
            origin=RateLocationRef(stated="Chennai (MAA)"),
            destination=RateLocationRef(stated="Singapore (SIN)"),
            weight_kg=500.0,
            dimensions_in=CargoDimensions(length=34, width=24, height=6),
            pieces=10,
            date=date(2026, 10, 5),
            commodity="Engineering components",
        ),
        filtered=filtered,
        selection=select_rate(filtered.eligible, FASTEST_ELIGIBLE),
    )
    return JobStatus(job_id=JOB_ID, state=JobState.COMPLETED, result=result)


ENQUIRY = _email(
    "<door-enq@client.example>",
    "Rate required - Chennai to Singapore - door delivery",
    "Complete enquiry.",
    TODAY - timedelta(hours=1),
)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    status: JobStatus,
    extraction: ExtractionResult,
    *,
    durable: InMemoryStore | None = None,
) -> tuple[LiveSession, QueueSpy, CollectingEmailSink, str]:
    queue = QueueSpy(fetch_returns=status)
    _install_queue(monkeypatch, queue)
    _forbid_demo_provider(monkeypatch)
    sink = CollectingEmailSink()
    session = _browser_session(
        _settings(WebCargoMode.BROWSER),
        source=StubSource(ENQUIRY),
        extractor=ScriptedExtractor(extraction),
        sink=sink,
        durable=durable,
    )
    session.poll()  # validated -> enqueued
    session.poll()  # job COMPLETED
    session.poll()
    session.poll()
    return session, queue, sink, next(iter(session.requests))


def _client_mail(sink: CollectingEmailSink) -> list[object]:
    return [m for m in sink.sent if m.to_address == CLIENT]


# --- 1. door + airport rates, excluded only for the unconfirmed door leg ------------------


def test_door_request_with_unconfirmed_door_rates_goes_to_a_person_not_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable = InMemoryStore()
    session, queue, sink, rid = _run(
        monkeypatch, _completed(AIRPORT_RATES, door=True), _door_extraction(), durable=durable
    )
    request = session.requests[rid]

    assert request.state is RequestState.MANUAL_REVIEW
    assert request.manual_review_notes == (DOOR_LEG_NOTE,)
    # The returned airport-to-airport rates are kept for the operator.
    assert request.rates is not None
    assert request.rates.returned == 2
    assert [e.rate.carrier_code for e in request.rates.filtered.excluded] == ["TK", "EK"]
    assert {e.reason for e in request.rates.filtered.excluded} == {
        ExclusionReason.SERVICE_NOT_AVAILABLE
    }
    # Nothing to the client, and nothing that could become a quotation.
    assert _client_mail(sink) == []
    assert sink.sent == []
    assert request.packet is None
    assert request.final_reply_sent is False
    # Durable, so a restart neither re-searches nor emails; searched once.
    stored = durable.get_request(rid)
    assert stored is not None
    assert stored.state is RequestState.MANUAL_REVIEW
    assert len(queue.enqueued) == 1


def test_the_door_hand_over_is_audited_with_the_returned_rates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _, _, rid = _run(monkeypatch, _completed(AIRPORT_RATES, door=True), _door_extraction())

    [event] = [
        e
        for e in session.audit.events
        if e.request_id == rid and e.event is AuditEventType.MANUAL_REVIEW_ESCALATED
    ]
    assert event.detail["reason"] == "door_leg_needs_manual_pricing"
    assert event.detail["notes"] == [DOOR_LEG_NOTE]
    assert event.detail["returned"] == 2
    assert event.detail["rates"] == [
        {
            "carrier": "Turkish Cargo",
            "total": "16900.00",
            "currency": "INR",
            "excluded": "service_not_available",
        },
        {
            "carrier": "Emirates",
            "total": "20762.00",
            "currency": "INR",
            "excluded": "service_not_available",
        },
    ]
    assert AuditEventType.NO_RATES_NOTICE_SENT not in {e.event for e in session.audit.events}


def test_the_door_hand_over_cannot_be_quoted_and_shows_as_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _, sink, rid = _run(
        monkeypatch, _completed(AIRPORT_RATES, door=True), _door_extraction()
    )

    with pytest.raises(LiveSequenceError):
        session.decide(rid, choice="approve", by="A. Operator")
    assert sink.sent == [], "no airport rate is quoted to a door-delivery client"

    snap = live_serialize.snapshot(session)
    [row] = snap["requests"]  # type: ignore[misc]
    assert row["request_id"] == rid, "active, for a person — not closed history"
    assert snap["history"] == []


class _Restarted:
    """A hand-over, then a real restart: a second `LiveSession` over the same
    durable store and settings, restored through `_restored_request`."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, rates: tuple[Rate, ...]) -> None:
        self.queue = QueueSpy(fetch_returns=_completed(rates, door=True))
        _install_queue(monkeypatch, self.queue)
        _forbid_demo_provider(monkeypatch)
        settings = _ops_browser()
        self.durable = InMemoryStore()
        self.first_sink = CollectingEmailSink()
        self.first = LiveSession(
            settings,
            source=StubSource(ENQUIRY),  # type: ignore[arg-type]
            sink=self.first_sink,
            extractor=ScriptedExtractor(_door_extraction()),  # type: ignore[arg-type]
            durable=self.durable,
        )
        self.first.poll()
        self.first.poll()
        [self.rid] = self.first.requests
        # Before the restart: the in-memory result, rows and all.
        self.before_rates = rates_json(self.first.requests[self.rid].rates)  # type: ignore[arg-type]
        self.before_notes = self.first.requests[self.rid].manual_review_notes

        self.second_sink = CollectingEmailSink()
        self.second = LiveSession(
            settings,
            source=StubSource(ENQUIRY),  # type: ignore[arg-type]
            sink=self.second_sink,
            extractor=ScriptedExtractor(),  # type: ignore[arg-type] - any model call fails
            durable=self.durable,
        )
        self.second.poll()
        self.second.poll()


def test_the_door_hand_over_survives_a_restart_with_its_reason_and_stays_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _Restarted(monkeypatch, AIRPORT_RATES)
    assert run.before_notes == (DOOR_LEG_NOTE,)

    stored = run.durable.get_request(run.rid)
    assert stored is not None
    assert stored.manual_review_notes == (DOOR_LEG_NOTE,), "the reason is persisted"

    restored = run.second.requests[run.rid]
    assert restored.state is RequestState.MANUAL_REVIEW
    assert restored.manual_review_notes == (DOOR_LEG_NOTE,), "and comes back after a restart"
    snap = live_serialize.snapshot(run.second)
    [row] = snap["requests"]  # type: ignore[misc]
    assert row["request_id"] == run.rid, "still Active, not history"
    assert row["manual_review_notes"] == [DOOR_LEG_NOTE]
    assert snap["history"] == []
    # Deliberately not persisted (option 2 was declined): the rows do not survive.
    assert restored.rates is None
    assert len(run.queue.enqueued) == 1, "not searched again after the restart"
    assert run.first_sink.sent == []
    assert run.second_sink.sent == []


# --- all returned rates explicitly reject door delivery (False, not unknown) --------------

DECLINING_RATES = (
    _rate("TK", "Turkish Cargo", "16900.00", door=False),
    _rate("EK", "Emirates", "20762.00", door=False),
)


def test_rates_that_all_decline_door_delivery_get_the_general_reason_not_the_door_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """False is "not door-capable", not "unknown": the operator must not be
    told these airport rates just need a door leg priced."""
    session, queue, sink, rid = _run(
        monkeypatch, _completed(DECLINING_RATES, door=True), _door_extraction()
    )
    request = session.requests[rid]

    assert request.state is RequestState.MANUAL_REVIEW
    [note] = request.manual_review_notes
    assert note != DOOR_LEG_NOTE
    assert "WebCargo returned 2 rate(s)" in note
    assert "the requested service is not offered" in note
    assert request.rates is not None
    assert request.rates.returned == 2, "the returned rates are retained"
    assert [e.detail for e in request.rates.filtered.excluded] == [
        "Turkish Cargo does not offer door delivery on this rate",
        "Emirates does not offer door delivery on this rate",
    ]
    assert sink.sent == []
    assert request.packet is None
    with pytest.raises(LiveSequenceError):
        session.decide(rid, choice="approve", by="A. Operator")
    snap = live_serialize.snapshot(session)
    [row] = snap["requests"]  # type: ignore[misc]
    assert row["request_id"] == rid
    assert snap["history"] == []
    assert len(queue.enqueued) == 1


def test_rates_that_all_decline_door_delivery_survive_a_restart_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _Restarted(monkeypatch, DECLINING_RATES)
    [note] = run.before_notes
    assert note != DOOR_LEG_NOTE

    restored = run.second.requests[run.rid]
    assert restored.state is RequestState.MANUAL_REVIEW
    assert restored.manual_review_notes == (note,)
    snap = live_serialize.snapshot(run.second)
    assert [r["request_id"] for r in snap["requests"]] == [run.rid]  # type: ignore[union-attr]
    assert len(run.queue.enqueued) == 1
    assert run.first_sink.sent == []
    assert run.second_sink.sent == []


# --- the excluded rows the operator works from (in-memory result, before restart) ---------


def test_excluded_rows_expose_the_returned_rate_details_before_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _Restarted(monkeypatch, AIRPORT_RATES)

    rows = run.before_rates["excluded"]
    assert [
        (r["carrier_name"], r["product"], r["amount"], r["currency"], r["transit"]) for r in rows
    ] == [
        ("Turkish Cargo", "GEN", "16900.00", "INR", "20 hours"),
        ("Emirates", "GEN", "20762.00", "INR", "20 hours"),
    ]
    assert {r["departure_date"] for r in rows} == {"Mon 05 Oct"}
    assert {r["reason"] for r in rows} == {"service_not_available"}
    # Only what the rate holds: no route or arrival is invented.
    assert not {"route", "itinerary", "arrival"} & set(rows[0])


def test_a_request_saved_before_the_notes_field_existed_still_loads() -> None:
    from translog_quote.domain.workflow import QuotationRequest

    stored = QuotationRequest(
        request_id="R-OLD",
        state=RequestState.MANUAL_REVIEW,
        record=_full_record("R-OLD", ship_date=date(2026, 10, 5)),
        client_address=CLIENT,
    ).model_dump(mode="json")
    stored.pop("manual_review_notes")

    assert QuotationRequest.model_validate(stored).manual_review_notes == ()


# --- 2. zero rows: the automatic notice is unchanged -------------------------------------


@pytest.mark.parametrize("door", [False, True], ids=["airport", "door"])
def test_zero_rows_still_sends_exactly_one_notice_and_closes(
    monkeypatch: pytest.MonkeyPatch, door: bool
) -> None:
    durable = InMemoryStore()
    session, queue, sink, rid = _run(
        monkeypatch,
        _completed((), door=door),
        _door_extraction() if door else _airport_extraction(),
        durable=durable,
    )
    request = session.requests[rid]

    assert request.state is RequestState.CLOSED_NO_RATES
    assert request.final_reply_sent is True
    assert request.packet is None
    notices = [m for m in sink.sent if m.to_address == CLIENT]
    assert len(notices) == 1, "exactly one client notification"
    assert "unable to source" in notices[0].body_text.lower()
    assert notices[0].in_reply_to == ENQUIRY.message_id
    stored = durable.get_request(rid)
    assert stored is not None
    assert stored.state is RequestState.CLOSED_NO_RATES
    assert len(queue.enqueued) == 1


# --- 3. rows returned, excluded for other reasons: to a person, in plain words ------------


def test_a_door_request_where_a_carrier_declines_door_delivery_gets_the_general_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not *only* an unconfirmed door leg: one carrier explicitly does not
    deliver to the door. Still rows returned, so still a person, no email — but
    not the door-leg note, which would claim both rates are usable."""
    rates = (
        _rate("TK", "Turkish Cargo", "16900.00", door=False),
        _rate("EK", "Emirates", "20762.00"),
    )
    session, _, sink, rid = _run(monkeypatch, _completed(rates, door=True), _door_extraction())
    request = session.requests[rid]

    assert request.state is RequestState.MANUAL_REVIEW
    assert request.manual_review_notes != (DOOR_LEG_NOTE,)
    [note] = request.manual_review_notes
    assert "WebCargo returned 2 rate(s)" in note
    assert "the requested service is not offered" in note
    assert "service_not_available" not in note, "plain words, not the internal code"
    assert sink.sent == []


def test_an_airport_request_whose_rates_are_all_unrankable_goes_to_a_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mapping-gap case: rates exist but lack a transit time. Emailing the
    client "no rates" here would pass a parsing problem off as a thin market."""
    rates = tuple(r.model_copy(update={"transit": None}) for r in AIRPORT_RATES)
    session, _, sink, rid = _run(monkeypatch, _completed(rates, door=False), _airport_extraction())
    request = session.requests[rid]

    assert request.state is RequestState.MANUAL_REVIEW
    [note] = request.manual_review_notes
    assert "no transit time to rank by" in note
    assert request.rates is not None
    assert request.rates.returned == 2
    assert sink.sent == []


def test_a_door_request_with_a_door_capable_rate_still_reaches_the_approval_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged: a rate that does confirm door delivery is selected and waits
    for a human decision, exactly as before."""
    rates = (
        _rate("TK", "Turkish Cargo", "16900.00", door=True),
        _rate("EK", "Emirates", "20762.00"),
    )
    session, _, sink, rid = _run(monkeypatch, _completed(rates, door=True), _door_extraction())
    request = session.requests[rid]

    assert request.state is RequestState.RATE_SELECTED
    assert request.packet is not None
    assert request.rates is not None
    assert request.rates.selection is not None
    assert request.rates.selection.rate.carrier_code == "TK"
    assert sink.sent == []
