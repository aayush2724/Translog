"""Every request in Active has a defined way out.

Two production dead-ends, closed without a parallel workflow:

1. **A validated request whose shipment date is in the past** (``5557b8344f``:
   2024-09-26, stored before the yearless-date fix) was refused by the rate
   gate on every poll, forever. It now takes the existing, operator-approved
   clarification path: VALIDATED -> NEEDS_INFO (a date question drafted, the
   stale date cleared in the working store) -> approval -> CLARIFICATION_SENT ->
   the client's reply -> the normal flow. Never auto-sent, never CLOSED_NO_RATES.

2. **MANUAL_REVIEW had no exit** (``66a07b1d2a``). Two changes:
   a. an *ambiguous shipment date* after a clarification (a date without its
      year) is re-asked for the complete date, under the ordinary round budget,
      instead of going straight to a person;
   b. a genuine hand-over can be closed by an operator: MANUAL_REVIEW -> RESOLVED
      (a new terminal state, History), named and audited, idempotent, sending
      nothing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.unit.test_clarification_loop import (
    REQ,
    ScriptedExtractor,
    _followup_workflow,
    _RecordingAudit,
    approve,
    complete,
    email,
)
from tests.unit.test_end_to_end_regressions import (
    APPROVER,
    CLIENT,
    TODAY,
    _email,
    _full_record,
)
from tests.unit.test_gmail_thread import StubSource
from tests.unit.test_live_browser_bridge import (
    QueueSpy,
    _forbid_demo_provider,
    _install_queue,
)
from tests.unit.test_reply_gate_and_extraction_isolation import (
    MovableClock,
    ProviderExtractor,
    _ops_browser,
)

from translog_quote import bootstrap
from translog_quote.adapters.clock import FixedClock
from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.store import InMemoryStore
from translog_quote.config import Settings
from translog_quote.domain.clarification import UnresolvedReason
from translog_quote.domain.conversation import Thread
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.shipment import CargoDimensions, FieldName
from translog_quote.domain.workflow import (
    TERMINAL_STATES,
    TRANSITIONS,
    QuotationRequest,
    RequestState,
)
from translog_quote.interface.web import live_serialize
from translog_quote.interface.web.live_session import LiveSequenceError, LiveSession
from translog_quote.pipeline import ClarificationWorkflow
from translog_quote.pipeline.audit import AuditEventType

PAST = date(2024, 9, 26)
RID = "R-GMAIL-pastship"
ENQ_ID = "<past-ship-enq@client.example>"


class Inbox:
    """A mailbox a test adds messages to between polls."""

    def __init__(self, *emails: RawEmail) -> None:
        self.emails = list(emails)

    def fetch_new(self, *, since: object = None) -> tuple[RawEmail, ...]:
        return tuple(self.emails)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> QueueSpy:
    queue = QueueSpy()
    _install_queue(monkeypatch, queue)
    _forbid_demo_provider(monkeypatch)
    return queue


def _seed(
    durable: InMemoryStore,
    request_id: str,
    state: RequestState,
    *,
    ship_date: date = PAST,
    message_id: str = ENQ_ID,
) -> None:
    """A committed request, with its thread, exactly as a real one is stored."""
    durable.save_request(
        QuotationRequest(
            request_id=request_id,
            state=state,
            record=_full_record(request_id, ship_date=ship_date),
            client_address=CLIENT,
        )
    )
    durable.save_thread(Thread(request_id=request_id, message_ids=(message_id,)))


def _live(
    settings: Settings,
    durable: InMemoryStore,
    *,
    source: object | None = None,
    extractor: ProviderExtractor | None = None,
) -> tuple[LiveSession, CollectingEmailSink]:
    sink = CollectingEmailSink()
    session = LiveSession(
        settings,
        source=source or StubSource(),  # type: ignore[arg-type]
        sink=sink,
        extractor=extractor or ProviderExtractor({}),  # type: ignore[arg-type]
        durable=durable,
        clock=MovableClock(TODAY),
    )
    return session, sink


def _events(session: LiveSession, request_id: str) -> list[AuditEventType]:
    return [e.event for e in session.audit.events if e.request_id == request_id]


def _to_client(sink: CollectingEmailSink) -> list[Any]:
    return [m for m in sink.sent if m.to_address == CLIENT]


def _ids(rows: object) -> list[str]:
    return [r["request_id"] for r in rows]  # type: ignore[attr-defined]


# --- Issue 1: a past shipment date asks the client, through approval ------------------------


def test_a_validated_past_date_drafts_a_date_question_and_sends_nothing(spy: QueueSpy) -> None:
    durable = InMemoryStore()
    _seed(durable, RID, RequestState.VALIDATED)
    session, sink = _live(_ops_browser(), durable)

    for _ in range(3):
        session.poll()

    request = session.requests[RID]
    assert spy.enqueued == [], "a past date is never searched"
    assert request.state is RequestState.NEEDS_INFO, "waiting on a person, not CLOSED_NO_RATES"
    assert request.rate_failure is None
    assert request.awaiting_clarification_approval
    assert request.clarification is not None
    [field] = request.clarification.unresolved
    assert field.field is FieldName.SHIP_DATE
    assert field.reason is UnresolvedReason.INVALID
    body = request.clarification.body_text
    assert "26 September 2024" in body
    assert "already in the past" in body
    assert "including the day, month and year" in body
    assert sink.sent == [], "never sent without a person"
    events = _events(session, RID)
    assert events.count(AuditEventType.SHIP_DATE_IN_PAST) == 1, "drafted once across polls"
    assert events.count(AuditEventType.CLARIFICATION_DRAFTED) == 1
    stored = durable.get_request(RID)
    assert stored is not None
    assert stored.state is RequestState.VALIDATED, "the draft is not committed"
    assert stored.record.ship_date == PAST
    assert _ids(live_serialize.snapshot(session)["requests"]) == [RID]


def test_a_restart_before_approval_re_derives_the_draft_without_sending(spy: QueueSpy) -> None:
    settings = _ops_browser()
    durable = InMemoryStore()
    _seed(durable, RID, RequestState.VALIDATED)
    first, first_sink = _live(settings, durable)
    first.poll()

    second, second_sink = _live(settings, durable)
    second.poll()
    second.poll()

    request = second.requests[RID]
    assert request.state is RequestState.NEEDS_INFO
    assert request.awaiting_clarification_approval
    assert first_sink.sent == []
    assert second_sink.sent == []
    assert spy.enqueued == []


def test_approval_sends_exactly_one_date_email_and_it_is_not_redrafted(spy: QueueSpy) -> None:
    settings = _ops_browser()
    durable = InMemoryStore()
    _seed(durable, RID, RequestState.VALIDATED)
    session, sink = _live(settings, durable)
    session.poll()

    session.approve_clarification(by=APPROVER, request_id=RID)
    for _ in range(3):
        session.poll()

    [sent] = _to_client(sink)
    assert "already in the past" in sent.body_text
    assert sent.in_reply_to == ENQ_ID
    events = _events(session, RID)
    assert events.count(AuditEventType.CLARIFICATION_SENT) == 1
    assert events.count(AuditEventType.SHIP_DATE_IN_PAST) == 1, "not redrafted after sending"
    stored = durable.get_request(RID)
    assert stored is not None
    assert stored.state is RequestState.CLARIFICATION_SENT
    assert stored.record.ship_date is None, "cleared, so the reply fills it"

    # A restart after the email went out: waiting on the client, no new draft.
    restarted, restarted_sink = _live(settings, durable)
    restarted.poll()
    assert restarted.requests[RID].state is RequestState.CLARIFICATION_SENT
    assert restarted_sink.sent == []


def _approved_then_reply(
    reply_date: date,
) -> tuple[LiveSession, CollectingEmailSink, InMemoryStore]:
    durable = InMemoryStore()
    _seed(durable, RID, RequestState.VALIDATED)
    reply = _email(
        "<past-ship-reply@client.example>",
        "Re: Rate required",
        "new date",
        TODAY - timedelta(minutes=10),
        in_reply_to=ENQ_ID,
    )
    inbox = Inbox()
    extractor = ProviderExtractor(
        {"new date": ExtractionResult(ship_date=ExtractedValue[date].stated(reply_date))}
    )
    session, sink = _live(_ops_browser(), durable, source=inbox, extractor=extractor)
    session.poll()
    session.approve_clarification(by=APPROVER, request_id=RID)
    inbox.emails.append(reply)
    session.poll()
    session.poll()
    return session, sink, durable


def test_the_clients_new_date_resumes_the_normal_flow(spy: QueueSpy) -> None:
    session, sink, durable = _approved_then_reply(date(2026, 10, 5))

    request = session.requests[RID]
    assert request.state is RequestState.VALIDATED
    assert request.record.ship_date == date(2026, 10, 5)
    assert len(spy.enqueued) == 1, "the search runs on the corrected date"
    assert len(_to_client(sink)) == 1, "only the one date question"
    stored = durable.get_request(RID)
    assert stored is not None
    assert stored.record.ship_date == date(2026, 10, 5)


def test_the_yearless_roll_forward_still_applies_to_the_reply(spy: QueueSpy) -> None:
    """Unchanged behaviour: a past date the model fills a year into is rolled
    forward at extraction, before any of the new path could see it."""
    session, _, _ = _approved_then_reply(date(2024, 10, 5))

    request = session.requests[RID]
    assert request.state is RequestState.VALIDATED
    assert request.record.ship_date == date(2026, 10, 5)
    assert AuditEventType.SHIP_DATE_YEAR_RESOLVED in _events(session, RID)
    assert len(spy.enqueued) == 1


def _validated_past_workflow(
    audit: _RecordingAudit,
) -> tuple[ClarificationWorkflow, CollectingEmailSink, InMemoryStore]:
    wf, sink, store = _followup_workflow(audit=audit)
    store.save_request(
        QuotationRequest(
            request_id=REQ,
            state=RequestState.VALIDATED,
            record=_full_record(REQ, ship_date=PAST),
            client_address=CLIENT,
        )
    )
    return wf, sink, store


def test_the_date_question_is_idempotent_while_pending() -> None:
    audit = _RecordingAudit()
    wf, sink, _ = _validated_past_workflow(audit)

    first = wf.request_ship_date_clarification(
        REQ, PAST, to_address=CLIENT, subject="Rate required", in_reply_to=ENQ_ID
    )
    second = wf.request_ship_date_clarification(
        REQ, PAST, to_address=CLIENT, subject="Rate required", in_reply_to=ENQ_ID
    )

    assert first is not None
    assert second is first
    assert wf._rounds[REQ] == 1
    assert [e.event for e in audit.events].count(AuditEventType.SHIP_DATE_IN_PAST) == 1
    assert sink.sent == []


def test_a_spent_budget_hands_the_past_date_to_a_person() -> None:
    audit = _RecordingAudit()
    wf, sink, store = _validated_past_workflow(audit)
    wf._rounds[REQ] = 3  # the default budget, already spent

    draft = wf.request_ship_date_clarification(
        REQ, PAST, to_address=CLIENT, subject="Rate required", in_reply_to=ENQ_ID
    )

    assert draft is None
    stored = store.get_request(REQ)
    assert stored is not None
    assert stored.state is RequestState.MANUAL_REVIEW
    [note] = stored.manual_review_notes
    assert "still in the past" in note
    assert "2024-09-26" in note
    escalations = [e for e in audit.events if e.event is AuditEventType.MANUAL_REVIEW_ESCALATED]
    assert escalations[-1].detail["reason"] == "ship_date_in_past_budget_exhausted"
    assert sink.sent == []


# --- Issue 2a: an ambiguous shipment date is re-asked, not handed over -------------------

AMBIGUOUS_DATE = ExtractedValue[date].ambiguous(
    note="The email provides a day and month but omits the year."
)


def test_an_ambiguous_date_reply_is_re_asked_for_the_complete_date() -> None:
    wf, sink, _ = _followup_workflow(
        complete(ship_date=AMBIGUOUS_DATE),
        ExtractionResult(ship_date=AMBIGUOUS_DATE),
    )
    first = wf.handle(REQ, email("ship on 26th September", n=1))
    assert first.state is RequestState.NEEDS_INFO
    approve(wf)

    second = wf.handle(REQ, email("26th September please", n=2))

    assert second.state is RequestState.NEEDS_INFO, "re-asked, not handed to a person"
    assert not second.needs_a_person
    assert second.clarification is not None
    [field] = second.clarification.unresolved
    assert field.field is FieldName.SHIP_DATE
    assert field.reason is UnresolvedReason.AMBIGUOUS
    assert "complete shipment date, including the day, month and year" in (
        second.clarification.body_text
    )
    assert len(sink.sent) == 1, "no automatic reminder; the re-ask waits for approval"


def test_a_complete_date_after_the_re_ask_converges() -> None:
    wf, _, _ = _followup_workflow(
        complete(ship_date=AMBIGUOUS_DATE),
        ExtractionResult(ship_date=AMBIGUOUS_DATE),
        ExtractionResult(ship_date=ExtractedValue[date].stated(date(2026, 9, 26))),
    )
    wf.handle(REQ, email("ship on 26th September", n=1))
    approve(wf)
    wf.handle(REQ, email("26th September please", n=2))
    approve(wf)

    answer = wf.handle(REQ, email("26 September 2026", n=3))

    assert answer.state is RequestState.VALIDATED
    assert answer.record.ship_date == date(2026, 9, 26)


def test_the_round_budget_still_ends_an_endlessly_ambiguous_date() -> None:
    wf = ClarificationWorkflow(
        extractor=ScriptedExtractor(
            complete(ship_date=AMBIGUOUS_DATE),
            ExtractionResult(ship_date=AMBIGUOUS_DATE),
            ExtractionResult(ship_date=AMBIGUOUS_DATE),
        ),
        sink=CollectingEmailSink(),
        store=InMemoryStore(),
        clock=FixedClock(),
        max_rounds=2,
    )
    wf.handle(REQ, email("ship on 26th September", n=1))
    approve(wf)
    wf.handle(REQ, email("26th September", n=2))
    approve(wf)

    last = wf.handle(REQ, email("the 26th", n=3))

    assert last.state is RequestState.MANUAL_REVIEW
    assert any("clarification rounds" in note for note in last.escalation_notes)


def test_other_ambiguous_fields_still_escalate_immediately() -> None:
    """Unchanged: two package sizes for one dimensions field is still a
    person's call, on the first unusable reply."""
    wf, _, _ = _followup_workflow(
        complete(dimensions_in=ExtractedValue[CargoDimensions].not_stated()),
        ExtractionResult(
            dimensions_in=ExtractedValue[CargoDimensions].ambiguous(
                note="Two different package sizes were given."
            )
        ),
    )
    wf.handle(REQ, email("no dimensions", n=1))
    approve(wf)

    outcome = wf.handle(REQ, email("30x20x10 and 40x30x20", n=2))

    assert outcome.state is RequestState.MANUAL_REVIEW


# --- Issue 2b: MANUAL_REVIEW -> RESOLVED -------------------------------------------------

MR = "R-GMAIL-handover"
MR_ENQ = "<handover-enq@client.example>"


def _handed_over() -> tuple[LiveSession, CollectingEmailSink, InMemoryStore, Settings]:
    settings = _ops_browser()
    durable = InMemoryStore()
    _seed(durable, MR, RequestState.MANUAL_REVIEW, ship_date=date(2026, 12, 1), message_id=MR_ENQ)
    session, sink = _live(settings, durable)
    return session, sink, durable, settings


def test_resolving_moves_the_request_to_history_records_it_and_sends_nothing(
    spy: QueueSpy,
) -> None:
    session, sink, durable, _ = _handed_over()
    assert _ids(live_serialize.snapshot(session)["requests"]) == [MR], "Active before"

    session.resolve_manual_review(MR, by="A. Operator", note="Priced by phone with the client.")

    request = session.requests[MR]
    assert request.state is RequestState.RESOLVED
    assert request.resolved_by == "A. Operator"
    assert request.resolved_at == TODAY
    assert request.resolution_note == "Priced by phone with the client."
    stored = durable.get_request(MR)
    assert stored is not None
    assert stored.state is RequestState.RESOLVED
    assert (stored.resolved_by, stored.resolved_at, stored.resolution_note) == (
        "A. Operator",
        TODAY,
        "Priced by phone with the client.",
    )
    [event] = [e for e in session.audit.events if e.event is AuditEventType.MANUAL_REVIEW_RESOLVED]
    assert event.at == TODAY
    assert event.detail == {
        "by": "A. Operator",
        "note": "Priced by phone with the client.",
        "from": "manual_review",
        "to": "resolved",
    }
    assert sink.sent == [], "no email on manual resolution"
    snap = live_serialize.snapshot(session, selected=MR)
    assert snap["requests"] == []
    assert _ids(snap["history"]) == [MR]
    detail: Any = snap["selected"]
    assert detail["status"]["label"] == "RESOLVED"
    assert detail["resolution"]["by"] == "A. Operator"


def test_resolving_twice_is_a_no_op(spy: QueueSpy) -> None:
    session, _, _, _ = _handed_over()
    session.resolve_manual_review(MR, by="A. Operator")
    first_at = session.requests[MR].resolved_at

    session.resolve_manual_review(MR, by="B. Someone Else", note="again")

    request = session.requests[MR]
    assert request.resolved_by == "A. Operator"
    assert request.resolved_at == first_at
    assert request.resolution_note is None
    resolved = [e for e in session.audit.events if e.event is AuditEventType.MANUAL_REVIEW_RESOLVED]
    assert len(resolved) == 1


def test_only_a_named_person_can_resolve_and_only_a_hand_over(spy: QueueSpy) -> None:
    session, _, durable, _ = _handed_over()
    with pytest.raises(LiveSequenceError, match="named"):
        session.resolve_manual_review(MR, by="   ")
    assert session.requests[MR].state is RequestState.MANUAL_REVIEW

    _seed(durable, "R-GMAIL-quoted", RequestState.QUOTATION_SENT, message_id="<q@c.example>")
    quoted, _ = _live(_ops_browser(), durable)
    with pytest.raises(LiveSequenceError, match="not awaiting manual review"):
        quoted.resolve_manual_review("R-GMAIL-quoted", by="A. Operator")
    assert quoted.requests["R-GMAIL-quoted"].state is RequestState.QUOTATION_SENT
    with pytest.raises(LiveSequenceError):
        quoted.resolve_manual_review("R-GMAIL-nope", by="A. Operator")


def test_a_resolved_request_survives_a_restart_in_history(spy: QueueSpy) -> None:
    session, _, durable, settings = _handed_over()
    session.resolve_manual_review(MR, by="A. Operator", note="done")

    restarted, _ = _live(settings, durable)

    request = restarted.requests[MR]
    assert request.state is RequestState.RESOLVED
    assert (request.resolved_by, request.resolution_note) == ("A. Operator", "done")
    snap = live_serialize.snapshot(restarted)
    assert snap["requests"] == []
    assert _ids(snap["history"]) == [MR]


def test_a_late_client_reply_to_a_resolved_request_costs_no_model_call(spy: QueueSpy) -> None:
    durable = InMemoryStore()
    _seed(durable, MR, RequestState.MANUAL_REVIEW, ship_date=date(2026, 12, 1), message_id=MR_ENQ)
    inbox = Inbox()
    extractor = ProviderExtractor({})  # any model call would raise KeyError
    session, sink = _live(_ops_browser(), durable, source=inbox, extractor=extractor)
    session.resolve_manual_review(MR, by="A. Operator")

    inbox.emails.append(
        _email("<late@c.example>", "Re: Rate", "any news?", TODAY, in_reply_to=MR_ENQ)
    )
    session.poll()

    assert extractor.calls == []
    assert session.requests[MR].state is RequestState.RESOLVED
    assert AuditEventType.REPLY_NOT_ACCEPTED in _events(session, MR)
    assert sink.sent == []


def test_other_terminal_states_are_unchanged() -> None:
    for state in (
        RequestState.CLOSED_NO_RATES,
        RequestState.MAKER_REJECTED,
        RequestState.FAILED,
        RequestState.ACCEPTED,
        RequestState.DECLINED,
    ):
        assert state in TERMINAL_STATES
        assert TRANSITIONS[state] == frozenset()
    assert TRANSITIONS[RequestState.QUOTATION_SENT] == frozenset(
        {RequestState.ACCEPTED, RequestState.DECLINED, RequestState.MANUAL_REVIEW}
    )


def test_resolution_is_routed_to_the_owning_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_multi_account_session import _write_accounts

    from translog_quote.config.settings import DemoSettings, GmailSettings, OpenRouterSettings
    from translog_quote.interface.web.multi_account_session import MultiAccountSession

    settings = Settings(
        openrouter=OpenRouterSettings(api_key="k"),  # type: ignore[arg-type]
        gmail=GmailSettings(
            accounts_dir=tmp_path / "config",
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
    for account in ("acct-a", "acct-b"):
        store = bootstrap.build_persistent_store(settings, account_id=account)
        rid = f"{account}:R-MR"
        store.save_request(
            QuotationRequest(
                request_id=rid,
                state=RequestState.MANUAL_REVIEW,
                record=_full_record(rid, ship_date=date(2026, 12, 1)),
                client_address=CLIENT,
            )
        )
        store.save_thread(Thread(request_id=rid, message_ids=(f"<{account}@c.example>",)))
    sinks = {"acct-a": CollectingEmailSink(), "acct-b": CollectingEmailSink()}
    monkeypatch.setattr(bootstrap, "build_extractor", lambda s: ProviderExtractor({}))
    monkeypatch.setattr(
        bootstrap, "build_gmail_email_sink", lambda s, *, account=None: sinks[account.account_id]
    )
    monkeypatch.setattr(
        bootstrap, "build_gmail_email_source", lambda s, *, account=None, **_: StubSource()
    )
    _write_accounts(tmp_path / "config", ("acct-a", True), ("acct-b", True))
    ms = MultiAccountSession.build(settings)

    ms.resolve_manual_review("acct-b:R-MR", by="A. Operator")

    assert ms.sessions["acct-b"].requests["acct-b:R-MR"].state is RequestState.RESOLVED
    assert ms.sessions["acct-a"].requests["acct-a:R-MR"].state is RequestState.MANUAL_REVIEW
    assert sinks["acct-a"].sent == []
    assert sinks["acct-b"].sent == []
    ms.close()


def test_the_resolve_endpoint_over_http(spy: QueueSpy) -> None:
    import threading

    from tests.unit.test_web_live import call

    from translog_quote.interface.web.server import DemoServer

    session, sink, _, settings = _handed_over()
    server = DemoServer(("127.0.0.1", 0), settings, live_session=session)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _ = call(
            server, "POST", "/api/live/manual-review/resolve", {"request_id": MR, "by": ""}
        )
        assert status == 409, "an unnamed resolution is refused"
        assert session.requests[MR].state is RequestState.MANUAL_REVIEW

        status, payload = call(
            server,
            "POST",
            "/api/live/manual-review/resolve",
            {"request_id": MR, "by": "A. Operator", "note": "handled"},
        )
        assert status == 200
        assert _ids(payload["history"]) == [MR]
        assert sink.sent == []
    finally:
        server.shutdown()
        server.server_close()
