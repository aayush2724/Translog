"""End-to-end regression pass over the production stack, before the UI work.

Every test here drives a real `LiveSession` (or `MultiAccountSession`): the real
inbound router, clarification workflow, validator, durable store, audit log and
browser-mode rate-search bridge. Only the boundaries are stubbed — the mailbox
(a scripted source), the model (a scripted extractor), outbound mail (a
collecting sink) and the Redis queue (a spy standing in for the worker) — so
each test proves behaviour of the code that actually runs in production.

Production runs browser mode, so these do too: a rate search is an *enqueue*
to the worker, and "reached WebCargo" means "a job was enqueued".
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.unit.test_gmail_thread import ScriptedExtractor, StubSource
from tests.unit.test_live_browser_bridge import (
    JOB_ID,
    QueueSpy,
    _forbid_demo_provider,
    _install_queue,
    _settings,
)
from tests.unit.test_operations_restart import _operations
from tests.unit.test_web_live import GrowingSource

from translog_quote import bootstrap
from translog_quote.adapters.clock import FixedClock
from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.store import InMemoryStore
from translog_quote.config import Settings, WebCargoMode
from translog_quote.config.settings import DemoSettings
from translog_quote.domain.clarification.questions import invalid_question
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.rates import (
    FASTEST_ELIGIBLE,
    Rate,
    RateQuery,
    filter_rates,
    select_rate,
)
from translog_quote.domain.rates import LocationRef as RateLocationRef
from translog_quote.domain.shipment import CargoDimensions, DeliveryType, FieldName
from translog_quote.domain.validation import ValidationRuleId, validate_shipment
from translog_quote.domain.workflow import QuotationRequest, RequestState
from translog_quote.interface.jobs import JobState, JobStatus, RateSearchJobResult
from translog_quote.interface.web import live_serialize
from translog_quote.interface.web.live_session import LiveSession
from translog_quote.pipeline.audit import AuditEventType

APPROVER = "ops.manager@translog.example"
#: Production "today" for these tests: the day of the yearless-date incident.
TODAY = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
CLIENT = "client@example.com"


def _email(
    message_id: str, subject: str, body: str, at: datetime, *, in_reply_to: str | None = None
) -> RawEmail:
    return RawEmail(
        message_id=message_id,
        from_address=CLIENT,
        subject=subject,
        body_text=body,
        received_at=at,
        in_reply_to=in_reply_to,
    )


def _extraction(**overrides: object) -> ExtractionResult:
    """A complete enquiry on a lane the real browser-mode resolver accepts
    (explicit IATA codes), General Cargo facts, and no ship date unless given."""
    base: dict[str, object] = {
        "origin": ExtractedValue[str].stated("Chennai (MAA)"),
        "destination": ExtractedValue[str].stated("Singapore (SIN)"),
        "weight_kg": ExtractedValue[float].stated(500.0),
        "dimensions_in": ExtractedValue[CargoDimensions].stated(
            CargoDimensions(length=34, width=24, height=6)
        ),
        "commodity": ExtractedValue[str].stated("Engineering components"),
        "cargo_type": ExtractedValue[str].stated("Non-Haz"),
        "is_chemical": ExtractedValue[bool].stated(value=False),
        "pcs": ExtractedValue[int].stated(10),
        "delivery_type": ExtractedValue[DeliveryType].stated(DeliveryType.AIRPORT),
    }
    base.update(overrides)
    return ExtractionResult(**base)  # type: ignore[arg-type]


def _full_record(request_id: str, *, ship_date: date) -> Any:
    from translog_quote.domain.shipment import RequestSource, ShipmentRecord

    return ShipmentRecord(
        request_id=request_id,
        source=RequestSource.EMAIL,
        origin="Chennai (MAA)",
        destination="Singapore (SIN)",
        weight_kg=500.0,
        dimensions_in=CargoDimensions(length=34, width=24, height=6),
        commodity="Engineering components",
        cargo_type="Non-Haz",
        is_chemical=False,
        pcs=10,
        delivery_type=DeliveryType.AIRPORT,
        ship_date=ship_date,
    )


def _browser_session(
    settings: Settings,
    *,
    source: object,
    extractor: ScriptedExtractor,
    sink: CollectingEmailSink,
    durable: object | None = None,
) -> LiveSession:
    return LiveSession(
        settings,
        source=source,  # type: ignore[arg-type]
        sink=sink,
        extractor=extractor,  # type: ignore[arg-type]
        durable=durable,  # type: ignore[arg-type]
        clock=FixedClock(TODAY),
    )


def _events_for(session: LiveSession, request_id: str) -> list[AuditEventType]:
    return [e.event for e in session.audit.events if e.request_id == request_id]


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> QueueSpy:
    queue = QueueSpy()
    _install_queue(monkeypatch, queue)
    _forbid_demo_provider(monkeypatch)
    return queue


# --- 1. Yearless shipment date, end to end ----------------------------------------

ENQ_1 = _email(
    "<yearless-enq@client.example>",
    "Rate required - Chennai to Singapore",
    "500 kg, 10 pcs, 34x24x6 in, engineering components, non-haz, airport to airport.",
    TODAY - timedelta(hours=3),
)
REPLY_1 = _email(
    "<yearless-reply@client.example>",
    "Re: Rate required - Chennai to Singapore",
    "26th September",
    TODAY - timedelta(hours=1),
    in_reply_to=ENQ_1.message_id,
)


def test_1_a_yearless_reply_date_is_resolved_audited_persisted_and_searched(
    spy: QueueSpy,
) -> None:
    """The production incident, replayed through the live stack. The enquiry has
    no date, so a clarification asks for one; the client answers "26th
    September" and the model (as it did in production) returns 2024-09-26.
    The request must validate on 2026-09-26, persist that date, record the
    correction before the merge, and enqueue a WebCargo search for 2026-09-26."""
    sink = CollectingEmailSink()
    durable = InMemoryStore()
    session = _browser_session(
        _settings(WebCargoMode.BROWSER),
        source=GrowingSource((ENQ_1,), (ENQ_1, REPLY_1)),
        extractor=ScriptedExtractor(
            _extraction(),
            ExtractionResult(
                ship_date=ExtractedValue[date].stated(date(2024, 9, 26), evidence="26th September")
            ),
        ),
        sink=sink,
        durable=durable,
    )

    session.poll()
    request_id = next(iter(session.requests))
    request = session.requests[request_id]
    assert request.state is RequestState.NEEDS_INFO
    assert request.clarification is not None
    assert [u.field for u in request.clarification.unresolved] == [FieldName.SHIP_DATE]

    session.approve_clarification(by=APPROVER, request_id=request_id)
    assert len(sink.sent) == 1, "the date question went to the client"
    assert spy.enqueued == [], "nothing is searched before the date is known"

    session.poll()
    request = session.requests[request_id]

    assert request.state is RequestState.VALIDATED
    assert request.record.ship_date == date(2026, 9, 26)
    stored = durable.get_request(request_id)
    assert stored is not None
    assert stored.record.ship_date == date(2026, 9, 26), "the resolved date is what persists"

    events = _events_for(session, request_id)
    assert events.count(AuditEventType.SHIP_DATE_YEAR_RESOLVED) == 1
    # The correction precedes the reply's merge (the enquiry's merge is earlier).
    resolved_at = events.index(AuditEventType.SHIP_DATE_YEAR_RESOLVED)
    merges = [i for i, e in enumerate(events) if e is AuditEventType.RECORD_MERGED]
    assert len(merges) == 2
    assert merges[0] < resolved_at < merges[1]
    detail = next(
        e.detail
        for e in session.audit.events
        if e.request_id == request_id and e.event is AuditEventType.SHIP_DATE_YEAR_RESOLVED
    )
    assert detail == {"from": "2024-09-26", "to": "2026-09-26"}

    # It proceeds to rate search, on the resolved date — exactly once.
    assert len(spy.enqueued) == 1
    assert spy.enqueued[0].search_date == date(2026, 9, 26)
    assert request.rate_job_id == JOB_ID
    assert request.rate_failure is None


# --- 2. Explicit past date ------------------------------------------------------------

PAST_RECORD_ID = "R-GMAIL-pastdate"


def test_2_a_stored_explicit_past_date_never_reaches_webcargo_and_is_not_rewritten(
    spy: QueueSpy,
) -> None:
    """The shape of the real bad request: a VALIDATED record already carrying
    2024-09-26 in the durable store (it predates the fix). After a restart in
    operations mode, repeated polls must never enqueue it, must show a visible
    reason on the dashboard and in VR-13, and must leave the stored date as-is."""
    settings = _operations(_settings(WebCargoMode.BROWSER), since=TODAY - timedelta(days=1))
    record = _full_record(PAST_RECORD_ID, ship_date=date(2024, 9, 26))
    durable = InMemoryStore()
    durable.save_request(
        QuotationRequest(
            request_id=PAST_RECORD_ID,
            state=RequestState.VALIDATED,
            record=record,
            client_address=CLIENT,
        )
    )
    session = _browser_session(
        settings,
        source=StubSource(),
        extractor=ScriptedExtractor(),
        sink=CollectingEmailSink(),
        durable=durable,
    )

    for _ in range(3):
        session.poll()

    request = session.requests[PAST_RECORD_ID]
    assert spy.enqueued == [], "a past date must never be sent to the worker"
    assert request.rate_job_id is None
    assert request.rate_failure is not None
    assert "2024-09-26" in request.rate_failure and "past" in request.rate_failure
    assert request.validation.invalid_fields == (FieldName.SHIP_DATE,)
    assert ValidationRuleId.SHIP_DATE_IN_PAST in {i.rule_id for i in request.validation.issues}

    # Not silently rewritten — in memory or in the durable store.
    assert request.record.ship_date == date(2024, 9, 26)
    stored = durable.get_request(PAST_RECORD_ID)
    assert stored is not None
    assert stored.record.ship_date == date(2024, 9, 26)
    assert AuditEventType.SHIP_DATE_YEAR_RESOLVED not in _events_for(session, PAST_RECORD_ID)

    # Visible to the operator on the dashboard row.
    snap = live_serialize.snapshot(session)
    rows: list[Any] = snap["requests"]  # type: ignore[assignment]
    row = next(r for r in rows if r["request_id"] == PAST_RECORD_ID)
    assert "past" in row["rate_failure"]


def test_2b_an_explicit_year_in_new_mail_is_rolled_forward_documented_limitation(
    spy: QueueSpy,
) -> None:
    """Pins the documented limitation rather than hiding it: the extraction
    contract carries no "year was stated" bit, so a client who explicitly
    writes "26 September 2024" in NEW mail is treated like a yearless date —
    rolled forward, never searched as a past date, and always audited (so the
    rewrite is visible, not silent)."""
    enquiry = _email(
        "<explicit-year@client.example>",
        "Rate required - Chennai to Singapore",
        "Ship on 26 September 2024.",
        TODAY - timedelta(hours=1),
    )
    session = _browser_session(
        _settings(WebCargoMode.BROWSER),
        source=StubSource(enquiry),
        extractor=ScriptedExtractor(
            _extraction(
                ship_date=ExtractedValue[date].stated(
                    date(2024, 9, 26), evidence="26 September 2024"
                )
            )
        ),
        sink=CollectingEmailSink(),
    )

    session.poll()

    request_id = next(iter(session.requests))
    assert AuditEventType.SHIP_DATE_YEAR_RESOLVED in _events_for(session, request_id)
    assert [job.search_date for job in spy.enqueued] == [date(2026, 9, 26)]


# --- 3. Invalid data -> clarification -> valid correction -> VALIDATED ------------------

ENQ_3 = _email(
    "<invalid-enq@client.example>",
    "Rate required - Chennai to Singapore",
    "-5 pieces, 0 kg ...",
    TODAY - timedelta(hours=3),
)
REPLY_3 = _email(
    "<invalid-reply@client.example>",
    "Re: Rate required - Chennai to Singapore",
    "Sorry: 10 pieces, 500 kg.",
    TODAY - timedelta(hours=1),
    in_reply_to=ENQ_3.message_id,
)


def test_3_invalid_values_are_clarified_then_a_valid_reply_converges_to_validated(
    spy: QueueSpy,
) -> None:
    """Non-positive pieces AND weight in the enquiry. One clarification must ask
    for valid values (not hand to a person, not send a failure notice); the
    corrected reply must converge to VALIDATED and reach rate search once."""
    sink = CollectingEmailSink()
    session = _browser_session(
        _settings(WebCargoMode.BROWSER),
        source=GrowingSource((ENQ_3,), (ENQ_3, REPLY_3)),
        extractor=ScriptedExtractor(
            _extraction(
                pcs=ExtractedValue[int].invalid(note="stated -5"),
                weight_kg=ExtractedValue[float].invalid(note="stated 0"),
                ship_date=ExtractedValue[date].stated(date(2026, 10, 5)),
            ),
            ExtractionResult(
                pcs=ExtractedValue[int].stated(10),
                weight_kg=ExtractedValue[float].stated(500.0),
            ),
        ),
        sink=sink,
    )

    session.poll()
    request_id = next(iter(session.requests))
    request = session.requests[request_id]
    assert request.state is RequestState.NEEDS_INFO
    assert request.clarification is not None
    asked = {u.field: u.question for u in request.clarification.unresolved}
    assert asked == {
        FieldName.WEIGHT_KG: invalid_question(FieldName.WEIGHT_KG),
        FieldName.PCS: invalid_question(FieldName.PCS),
    }

    session.approve_clarification(by=APPROVER, request_id=request_id)
    session.poll()
    request = session.requests[request_id]

    assert request.state is RequestState.VALIDATED
    assert request.record.pcs == 10 and request.record.weight_kg == 500.0
    events = _events_for(session, request_id)
    assert AuditEventType.MANUAL_REVIEW_ESCALATED not in events
    assert AuditEventType.FAILURE_NOTICE_SENT not in events
    assert len(sink.sent) == 1, "exactly one email: the clarification — no failure notice"
    assert len(spy.enqueued) == 1
    assert spy.enqueued[0].pieces == 10 and spy.enqueued[0].weight_kg == 500.0


# --- 5. Restart / persistence ---------------------------------------------------------

ENQ_5 = _email(
    "<restart-enq@client.example>",
    "Rate required - Chennai to Singapore",
    "500 kg Chennai to Singapore, shipping 5 October.",
    TODAY - timedelta(hours=3),
)
REPLY_5 = _email(
    "<restart-reply@client.example>",
    "Re: Rate required - Chennai to Singapore",
    "10 pieces.",
    TODAY - timedelta(minutes=30),
    in_reply_to=ENQ_5.message_id,
)


def test_5_a_request_awaiting_the_client_survives_a_restart_and_nothing_is_reprocessed(
    spy: QueueSpy,
) -> None:
    """Operations mode, on the real on-disk state (JSON store, audit log and
    watermark file). A request whose clarification went out is waiting on the
    client. A brand-new session over the same state must restore the request,
    its thread, the watermark and its audit history; must not re-extract,
    re-draft or re-send for the enquiry it already handled; and must then take
    the client's reply exactly once."""
    settings = _operations(_settings(WebCargoMode.BROWSER), since=TODAY - timedelta(days=1))

    sink_1 = CollectingEmailSink()
    first = _browser_session(
        settings,
        source=StubSource(ENQ_5),
        extractor=ScriptedExtractor(
            _extraction(
                pcs=ExtractedValue[int].not_stated(),
                ship_date=ExtractedValue[date].stated(date(2026, 10, 5)),
            )
        ),
        sink=sink_1,
    )
    first.poll()
    request_id = next(iter(first.requests))
    first.approve_clarification(by=APPROVER, request_id=request_id)
    first.poll()
    assert first.requests[request_id].state is RequestState.CLARIFICATION_SENT
    assert len(sink_1.sent) == 1
    watermark = first.demonstration.last_poll_watermark
    record_before = first.requests[request_id].record
    audit_before = _events_for(first, request_id)
    first.close()

    # --- restart: new objects, same state_dir -------------------------------------
    extractor_2 = ScriptedExtractor(
        ExtractionResult(pcs=ExtractedValue[int].stated(10))  # the reply, only
    )
    sink_2 = CollectingEmailSink()
    second = _browser_session(
        settings,
        source=GrowingSource((ENQ_5,), (ENQ_5, REPLY_5), (ENQ_5, REPLY_5)),
        extractor=extractor_2,
        sink=sink_2,
    )

    restored = second.requests[request_id]
    assert restored.state is RequestState.CLARIFICATION_SENT
    assert restored.record == record_before
    [thread] = [t for t in second._durable.all_threads() if t.request_id == request_id]
    assert ENQ_5.message_id in thread.message_ids
    assert second.demonstration.last_poll_watermark == watermark
    audit_after = _events_for(second, request_id)
    assert audit_after[: len(audit_before)] == audit_before, "audit history survived"
    assert AuditEventType.CLARIFICATION_SENT in audit_after

    # Poll with only the already-handled enquiry in the mailbox: nothing happens.
    second.poll()
    assert extractor_2.calls == [], "the handled enquiry was not re-extracted"
    assert sink_2.sent == [], "nothing was re-sent"
    assert second.requests[request_id].state is RequestState.CLARIFICATION_SENT

    # The client's reply arrives: taken exactly once, and the request moves on.
    second.poll()
    second.poll()
    assert extractor_2.calls == [REPLY_5.body_text]
    assert second.requests[request_id].state is RequestState.VALIDATED
    assert len(spy.enqueued) == 1
    assert sink_2.sent == []
    second.close()


# --- 6. No-rate path through the browser worker result --------------------------------


def _zero_rows_status() -> JobStatus:
    """A COMPLETED worker result where WebCargo returned no rows at all — the
    only case that still earns the automatic "no rates" client notice. (Rows
    that came back but were all excluded go to a person instead; see
    ``test_door_leg_hand_over``.)"""
    rates: tuple[Rate, ...] = ()
    filtered = filter_rates(rates)
    assert filtered.eligible == () and filtered.excluded == ()
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


def test_6_zero_worker_rows_close_with_exactly_one_client_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Browser mode: the worker's COMPLETED result has no rows at all.
    The request must close at CLOSED_NO_RATES, the client must get exactly one
    "no rates" notice (in the enquiry's thread), no quotation packet may exist,
    and further polls must neither re-notify nor re-search."""
    queue = QueueSpy(fetch_returns=_zero_rows_status())
    _install_queue(monkeypatch, queue)
    _forbid_demo_provider(monkeypatch)
    enquiry = _email(
        "<norates-enq@client.example>",
        "Rate required - Chennai to Singapore",
        "Complete enquiry.",
        TODAY - timedelta(hours=1),
    )
    sink = CollectingEmailSink()
    durable = InMemoryStore()
    session = _browser_session(
        _settings(WebCargoMode.BROWSER),
        source=StubSource(enquiry),
        extractor=ScriptedExtractor(
            _extraction(ship_date=ExtractedValue[date].stated(date(2026, 10, 5)))
        ),
        sink=sink,
        durable=durable,
    )

    session.poll()  # validated -> enqueued
    request_id = next(iter(session.requests))
    assert len(queue.enqueued) == 1
    session.poll()  # job COMPLETED with nothing eligible
    session.poll()
    session.poll()

    request = session.requests[request_id]
    assert request.state is RequestState.CLOSED_NO_RATES
    assert request.packet is None
    notices = [m for m in sink.sent if m.to_address == CLIENT]
    assert len(notices) == 1, "exactly one client notification"
    assert notices[0].in_reply_to == enquiry.message_id
    assert len(queue.enqueued) == 1, "a closed request is never searched again"
    stored = durable.get_request(request_id)
    assert stored is not None and stored.state is RequestState.CLOSED_NO_RATES
    snap = live_serialize.snapshot(session)
    history: list[Any] = snap["history"]  # type: ignore[assignment]
    assert [r["request_id"] for r in history] == [request_id]
    assert snap["requests"] == []


# --- 7. Multi-account routing, end to end --------------------------------------------


class _KeyedExtractor:
    """One model for every account, answering by message body."""

    def __init__(self, by_body: dict[str, ExtractionResult]) -> None:
        self._by_body = by_body
        self.calls: list[str] = []

    def extract_shipment(self, text: str) -> ExtractionResult:
        self.calls.append(text)
        return self._by_body[text]

    def read_client_intent(self, text: str) -> object:
        raise NotImplementedError


def test_7_two_mailboxes_are_processed_independently_and_answer_from_their_own_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Account A and B each receive an incomplete enquiry. Polled through the
    real MultiAccountSession: each becomes a request namespaced to its own
    account; the unified dashboard shows both, tagged; approving each
    clarification sends it only through the originating mailbox, threaded to
    that mailbox's message — never through the sibling."""
    from tests.unit.test_multi_account_session import _write_accounts

    from translog_quote.config.settings import GmailSettings, OpenRouterSettings
    from translog_quote.interface.web.multi_account_session import MultiAccountSession

    enq_a = _email(
        "<a-enq@client-a.example>",
        "Rate required - A",
        "enquiry for A",
        datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
    )
    enq_b = _email(
        "<b-enq@client-b.example>",
        "Rate required - B",
        "enquiry for B",
        datetime(2026, 9, 22, 9, 5, tzinfo=UTC),
    )
    missing_pcs = _extraction(
        pcs=ExtractedValue[int].not_stated(),
        ship_date=ExtractedValue[date].stated(date(2026, 12, 1)),
    )
    extractor = _KeyedExtractor({"enquiry for A": missing_pcs, "enquiry for B": missing_pcs})
    sources = {"acct-a": StubSource(enq_a), "acct-b": StubSource(enq_b)}
    sinks: dict[str, CollectingEmailSink] = {}

    def build_sink(settings: Settings, *, account: Any = None) -> CollectingEmailSink:
        sinks[account.account_id] = CollectingEmailSink()
        return sinks[account.account_id]

    monkeypatch.setattr(bootstrap, "build_extractor", lambda settings: extractor)
    monkeypatch.setattr(bootstrap, "build_gmail_email_sink", build_sink)
    monkeypatch.setattr(
        bootstrap,
        "build_gmail_email_source",
        lambda settings, *, account=None, **_: sources[account.account_id],
    )
    accounts_dir = tmp_path / "config"
    _write_accounts(accounts_dir, ("acct-a", True), ("acct-b", True))
    settings = Settings(
        openrouter=OpenRouterSettings(api_key="k"),  # type: ignore[arg-type]
        gmail=GmailSettings(
            accounts_dir=accounts_dir,
            test_address="solo@example.com",
            approver_address="ops@example.com",
            send_enabled=True,
        ),
        demo=DemoSettings(
            state_dir=tmp_path / "state",
            startup_mode="operations",
            operations_since=datetime(2026, 9, 20, tzinfo=UTC),
        ),
    )
    ms = MultiAccountSession.build(settings)

    ms.poll()

    a_ids = list(ms.sessions["acct-a"].requests)
    b_ids = list(ms.sessions["acct-b"].requests)
    assert len(a_ids) == 1 and a_ids[0].startswith("acct-a:")
    assert len(b_ids) == 1 and b_ids[0].startswith("acct-b:")
    assert sorted(extractor.calls) == ["enquiry for A", "enquiry for B"]

    snap = live_serialize.snapshot(ms)
    rows: list[Any] = snap["requests"]  # type: ignore[assignment]
    assert {r["request_id"]: r["account"] for r in rows} == {
        a_ids[0]: "acct-a",
        b_ids[0]: "acct-b",
    }

    ms.approve_clarification(by=APPROVER, request_id=b_ids[0])
    assert sinks["acct-a"].sent == []
    assert [m.in_reply_to for m in sinks["acct-b"].sent] == [enq_b.message_id]

    ms.approve_clarification(by=APPROVER, request_id=a_ids[0])
    assert [m.in_reply_to for m in sinks["acct-a"].sent] == [enq_a.message_id]
    assert len(sinks["acct-b"].sent) == 1, "B's mailbox sent nothing on A's behalf"
    assert ms.sessions["acct-a"].requests[a_ids[0]].state is RequestState.CLARIFICATION_SENT
    assert ms.sessions["acct-b"].requests[b_ids[0]].state is RequestState.CLARIFICATION_SENT
    ms.close()


def test_2_fixture_guard_the_past_record_is_otherwise_complete() -> None:
    """Guard for test 2's fixture: the stored record is otherwise complete, so the
    ONLY thing keeping it from WebCargo is the past date."""
    record = _full_record("R-x", ship_date=date(2024, 9, 26))
    assert validate_shipment(record).is_valid
    assert not validate_shipment(record, today=TODAY.date()).is_valid
