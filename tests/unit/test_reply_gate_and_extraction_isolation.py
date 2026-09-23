"""The two defects behind the 2026-09-23 OpenRouter outage, pinned.

1. A client reply to a request that could no longer take one (VALIDATED, quoted,
   closed, with a person) was extracted — a paid model call — and only then
   refused by the state machine. Nothing recorded it, so every poll fetched and
   extracted it again: 199 of the day's 218 model calls, until the key's spend
   limit was hit (HTTP 402). The workflow now refuses such a reply *before* the
   model, and the caller records it so it is never read again.

2. A provider failure during one message's extraction escaped the whole account
   poll, so every newer message, every rate search and the watermark stalled
   behind it. The failure is now translated at the single model call into
   `ExtractionUnavailable` and isolated to that message.

Driven through the production stack (`LiveSession` / `MultiAccountSession`,
real router, workflow, stores, audit and browser-mode rate bridge); only the
mailbox, model, mail sink and job queue are stubbed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.test_clarification_loop import REQ, approve, complete, email, workflow
from tests.unit.test_end_to_end_regressions import (
    APPROVER,
    CLIENT,
    TODAY,
    _email,
    _events_for,
    _extraction,
    _full_record,
)
from tests.unit.test_gmail_thread import StubSource
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
from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.store import InMemoryStore
from translog_quote.config import Settings, WebCargoMode
from translog_quote.config.settings import DemoSettings
from translog_quote.domain.conversation import Thread
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.workflow import QuotationRequest, RequestState
from translog_quote.errors import IllegalTransition, PermanentFailure, TransientFailure
from translog_quote.interface.jobs import JobState, JobStatus
from translog_quote.interface.web.live_session import EXTRACTION_RETRY_AFTER, LiveSession
from translog_quote.pipeline.audit import AuditEventType

SHIP = ExtractedValue[date].stated(date(2026, 10, 5))


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> QueueSpy:
    """The Redis queue replaced by a spy; browser mode never builds the demo provider."""
    queue = QueueSpy()
    _install_queue(monkeypatch, queue)
    _forbid_demo_provider(monkeypatch)
    return queue


#: The provider's refusal exactly as the transport raises it in production.
HTTP_402 = PermanentFailure(
    "OpenRouter returned 402: This request requires more credits, or fewer max_tokens."
)


class ProviderExtractor:
    """A model that answers per message body, or fails the way a provider does.

    Counts every call — the thing these tests are really about is how many
    paid calls a poll makes."""

    def __init__(self, answers: dict[str, ExtractionResult | Exception]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def extract_shipment(self, text: str) -> ExtractionResult:
        self.calls.append(text)
        answer = self.answers[text]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def read_client_intent(self, text: str) -> object:
        raise NotImplementedError


class MovableClock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


def _session(
    settings: Settings,
    *,
    source: object,
    extractor: ProviderExtractor,
    durable: InMemoryStore | None = None,
    clock: MovableClock | None = None,
) -> LiveSession:
    return LiveSession(
        settings,
        source=source,  # type: ignore[arg-type]
        sink=CollectingEmailSink(),
        extractor=extractor,  # type: ignore[arg-type]
        durable=durable,
        clock=clock or MovableClock(TODAY),
    )


def _committed(durable: InMemoryStore, message_id: str) -> bool:
    return any(message_id in t.message_ids for t in durable.all_threads())


def _restored(
    durable: InMemoryStore, request_id: str, state: RequestState, enquiry: RawEmail
) -> None:
    """A request already durable in `state`, with its enquiry in its thread —
    the shape a restart restores (the production 960c/ff50 requests)."""
    durable.save_request(
        QuotationRequest(
            request_id=request_id,
            state=state,
            record=_full_record(request_id, ship_date=date(2026, 10, 5)),
            client_address=CLIENT,
        )
    )
    durable.save_thread(Thread(request_id=request_id, message_ids=(enquiry.message_id,)))


def _ops_browser() -> Settings:
    return _operations(_settings(WebCargoMode.BROWSER), since=TODAY - timedelta(days=1))


# --- A. reply to a VALIDATED request ---------------------------------------------------

ENQ_A = _email(
    "<a-enq@c.example>",
    "Rate required - Chennai to Singapore",
    "enquiry A",
    TODAY - timedelta(hours=2),
)
REPLY_A = _email(
    "<a-reply@c.example>",
    "Re: Rate required - Chennai to Singapore",
    "any update on this?",
    TODAY - timedelta(hours=1),
    in_reply_to=ENQ_A.message_id,
)


def test_a_reply_to_a_validated_request_is_consumed_without_a_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A job that stays queued, so any second enqueue would be the reply's doing.
    queue = QueueSpy(fetch_returns=JobStatus(job_id=JOB_ID, state=JobState.QUEUED))
    _install_queue(monkeypatch, queue)
    _forbid_demo_provider(monkeypatch)
    extractor = ProviderExtractor({"enquiry A": _extraction(ship_date=SHIP)})
    durable = InMemoryStore()
    session = _session(
        _settings(WebCargoMode.BROWSER),
        source=GrowingSource((ENQ_A,), (ENQ_A, REPLY_A)),
        extractor=extractor,
        durable=durable,
    )
    session.poll()
    request_id = next(iter(session.requests))
    before = session.requests[request_id]
    assert before.state is RequestState.VALIDATED
    record_before = before.record

    for _ in range(3):
        session.poll()

    assert extractor.calls == ["enquiry A"], "the reply never reached the model"
    assert _committed(durable, REPLY_A.message_id), "consumed durably"
    assert session._seen_message(REPLY_A.message_id)
    events = _events_for(session, request_id)
    assert events.count(AuditEventType.REPLY_NOT_ACCEPTED) == 1, "handled once, not per poll"
    after = session.requests[request_id]
    assert after.state is RequestState.VALIDATED
    assert after.record == record_before
    assert list(session.requests) == [request_id], "no new request"
    assert len(queue.enqueued) == 1, "the rate search is not re-run by the reply"
    assert session.last_poll_error is None


# --- B. reply after the quotation was sent (and other closed states) -------------------


@pytest.mark.parametrize(
    "state",
    [
        RequestState.QUOTATION_SENT,
        RequestState.MANUAL_REVIEW,
        RequestState.CLOSED_NO_RATES,
        RequestState.MAKER_REJECTED,
    ],
)
def test_b_a_reply_to_a_closed_or_quoted_request_is_consumed_without_a_model_call(
    spy: QueueSpy, state: RequestState
) -> None:
    enquiry = _email(
        "<b-enq@c.example>", "Rate required", "old enquiry", TODAY - timedelta(days=1, hours=1)
    )
    reply = _email(
        "<b-reply@c.example>",
        "Re: Rate required",
        "thanks, one more question",
        TODAY - timedelta(minutes=30),
        in_reply_to=enquiry.message_id,
    )
    durable = InMemoryStore()
    _restored(durable, "R-B", state, enquiry)
    extractor = ProviderExtractor({})  # any model call would raise KeyError
    session = _session(
        _ops_browser(), source=StubSource(enquiry, reply), extractor=extractor, durable=durable
    )

    session.poll()
    session.poll()

    assert extractor.calls == []
    assert _committed(durable, reply.message_id)
    assert set(session.requests) == {"R-B"}, "no new request was created"
    stored = durable.get_request("R-B")
    assert stored is not None
    assert stored.state is state, "request state untouched"
    assert _events_for(session, "R-B").count(AuditEventType.REPLY_NOT_ACCEPTED) == 1


# --- C. a genuine clarification reply still goes to the model ------------------------------

ENQ_C = _email(
    "<c-enq@c.example>",
    "Rate required - Chennai to Singapore",
    "enquiry C",
    TODAY - timedelta(hours=2),
)
REPLY_C = _email(
    "<c-reply@c.example>",
    "Re: Rate required - Chennai to Singapore",
    "10 pieces",
    TODAY - timedelta(hours=1),
    in_reply_to=ENQ_C.message_id,
)


def test_c_a_reply_to_a_clarification_is_extracted_and_resolves_normally(spy: QueueSpy) -> None:
    extractor = ProviderExtractor(
        {
            "enquiry C": _extraction(pcs=ExtractedValue[int].not_stated(), ship_date=SHIP),
            "10 pieces": ExtractionResult(pcs=ExtractedValue[int].stated(10)),
        }
    )
    session = _session(
        _settings(WebCargoMode.BROWSER),
        source=GrowingSource((ENQ_C,), (ENQ_C, REPLY_C), (ENQ_C, REPLY_C)),
        extractor=extractor,
    )
    session.poll()
    request_id = next(iter(session.requests))
    assert session.requests[request_id].state is RequestState.NEEDS_INFO

    # While the draft is unsent the reply waits — and costs nothing.
    session.poll()
    assert extractor.calls == ["enquiry C"]
    assert session.requests[request_id].waiting_replies == [REPLY_C.message_id]

    session.approve_clarification(by=APPROVER, request_id=request_id)
    session.poll()

    assert extractor.calls == ["enquiry C", "10 pieces"], "the genuine answer IS extracted"
    assert session.requests[request_id].state is RequestState.VALIDATED
    assert session.requests[request_id].record.pcs == 10
    assert AuditEventType.REPLY_NOT_ACCEPTED not in _events_for(session, request_id)
    assert len(spy.enqueued) == 1


def test_c_the_workflow_refuses_an_unsent_draft_reply_before_the_model() -> None:
    """The same guarantee below the live session, for any caller of `handle`:
    a reply to a NEEDS_INFO request raises the same IllegalTransition as
    before — but now before the model call, not after it."""
    wf, _ = workflow(complete(pcs=ExtractedValue[int].not_stated()))
    first = wf.handle(REQ, email("no pieces", n=1))
    assert first.state is RequestState.NEEDS_INFO
    calls_before = len(wf._extractor.calls)  # type: ignore[attr-defined]

    with pytest.raises(IllegalTransition):
        wf.handle(REQ, email("10 pieces", n=2))

    assert len(wf._extractor.calls) == calls_before  # type: ignore[attr-defined]
    approve(wf)  # the draft still goes out normally afterwards


# --- D. one message's 402 does not stop the poll -------------------------------------------

BAD = _email(
    "<d-bad@c.example>", "Rate required - long thread", "huge email", TODAY - timedelta(hours=3)
)
GOOD = _email(
    "<d-good@c.example>",
    "Rate required - Chennai to Singapore",
    "short enquiry",
    TODAY - timedelta(hours=1),
)


def test_d_a_402_on_one_message_does_not_abort_the_poll(spy: QueueSpy) -> None:
    extractor = ProviderExtractor(
        {"huge email": HTTP_402, "short enquiry": _extraction(ship_date=SHIP)}
    )
    durable = InMemoryStore()
    session = _session(
        _settings(WebCargoMode.BROWSER),
        source=StubSource(BAD, GOOD),
        extractor=extractor,
        durable=durable,
    )

    session.poll()  # must not raise

    # The older message failed first, and the newer one was still processed —
    # all the way to a rate search, which used to be skipped with it.
    assert extractor.calls == ["huge email", "short enquiry"]
    [request] = session.requests.values()
    assert request.state is RequestState.VALIDATED
    assert len(spy.enqueued) == 1
    # The failed message is neither consumed nor a request, and is reported.
    assert not _committed(durable, BAD.message_id)
    assert not session._seen_message(BAD.message_id)
    assert session.last_poll_error == "ExtractionUnavailable"
    failed = [e for e in session.audit.events if e.event is AuditEventType.EXTRACTION_UNAVAILABLE]
    assert [e.detail for e in failed] == [{"error": "PermanentFailure", "permanent": True}]


def test_d_a_permanently_refused_message_is_held_then_retried_once_the_hold_lapses(
    spy: QueueSpy,
) -> None:
    clock = MovableClock(TODAY)
    extractor = ProviderExtractor({"huge email": HTTP_402})
    session = _session(
        _settings(WebCargoMode.BROWSER), source=StubSource(BAD), extractor=extractor, clock=clock
    )

    session.poll()
    for _ in range(5):  # a poll every few seconds
        clock.at += timedelta(seconds=15)
        session.poll()
    assert extractor.calls == ["huge email"], "no tight retry loop against a refusing key"
    assert session.last_poll_error == "ExtractionUnavailable", "still reported while held"

    # The key is topped up; after the hold the message is tried again, and works.
    extractor.answers["huge email"] = _extraction(ship_date=SHIP)
    clock.at = TODAY + EXTRACTION_RETRY_AFTER + timedelta(seconds=1)
    session.poll()

    assert extractor.calls == ["huge email", "huge email"]
    assert len(session.requests) == 1
    assert session.last_poll_error is None, "recovered, and says so"


def test_d_a_transient_failure_is_retried_on_the_next_poll_not_held(spy: QueueSpy) -> None:
    extractor = ProviderExtractor({"huge email": TransientFailure("OpenRouter timed out")})
    session = _session(_settings(WebCargoMode.BROWSER), source=StubSource(BAD), extractor=extractor)

    session.poll()
    session.poll()

    assert extractor.calls == ["huge email", "huge email"]
    assert session.last_poll_error == "ExtractionUnavailable"


def test_d_an_unrelated_programming_error_is_not_swallowed(spy: QueueSpy) -> None:
    """The isolation is scoped to the provider boundary: anything else that
    breaks in a turn still escapes the poll, loudly, as before."""
    extractor = ProviderExtractor({"huge email": RuntimeError("a real bug")})
    session = _session(_settings(WebCargoMode.BROWSER), source=StubSource(BAD), extractor=extractor)

    with pytest.raises(RuntimeError):
        session.poll()


# --- E. account isolation ------------------------------------------------------------------


def test_e_account_a_extraction_failure_does_not_stop_account_b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_multi_account_session import _write_accounts

    from translog_quote.config.settings import GmailSettings, OpenRouterSettings
    from translog_quote.interface.web.multi_account_session import MultiAccountSession

    enq_a = _email(
        "<e-a@c.example>",
        "Rate required - A",
        "enquiry for A",
        datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
    )
    enq_b = _email(
        "<e-b@c.example>",
        "Rate required - B",
        "enquiry for B",
        datetime(2026, 9, 22, 9, 5, tzinfo=UTC),
    )
    extractor = ProviderExtractor(
        {
            "enquiry for A": HTTP_402,
            "enquiry for B": _extraction(
                pcs=ExtractedValue[int].not_stated(),
                ship_date=ExtractedValue[date].stated(date(2026, 12, 1)),
            ),
        }
    )
    sources = {"acct-a": StubSource(enq_a), "acct-b": StubSource(enq_b)}
    monkeypatch.setattr(bootstrap, "build_extractor", lambda settings: extractor)
    monkeypatch.setattr(
        bootstrap, "build_gmail_email_sink", lambda settings, *, account=None: CollectingEmailSink()
    )
    monkeypatch.setattr(
        bootstrap,
        "build_gmail_email_source",
        lambda settings, *, account=None, **_: sources[account.account_id],
    )
    _write_accounts(tmp_path / "config", ("acct-a", True), ("acct-b", True))
    ms = MultiAccountSession.build(
        Settings(
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
    )

    ms.poll()

    assert ms.sessions["acct-a"].requests == {}
    [b_request] = ms.sessions["acct-b"].requests.values()
    assert b_request.state is RequestState.NEEDS_INFO
    assert ms.sessions["acct-a"].last_poll_error == "ExtractionUnavailable"
    assert ms.sessions["acct-b"].last_poll_error is None
    assert ms.last_poll_error == "ExtractionUnavailable", "the desk still sees A's outage"
    ms.close()


# --- F. watermark semantics ----------------------------------------------------------------


def test_f_the_watermark_holds_at_a_failed_message_and_advances_once_it_is_handled(
    spy: QueueSpy,
) -> None:
    clock = MovableClock(TODAY)
    extractor = ProviderExtractor(
        {"huge email": HTTP_402, "short enquiry": _extraction(ship_date=SHIP)}
    )
    durable = InMemoryStore()
    session = _session(
        _ops_browser(),
        source=StubSource(BAD, GOOD),
        extractor=extractor,
        durable=durable,
        clock=clock,
    )

    session.poll()

    # GOOD (newer) is committed; BAD (older) is not, so the cutoff holds at BAD
    # rather than jumping past it to GOOD — a restart would still re-read it.
    assert _committed(durable, GOOD.message_id)
    assert session.demonstration.last_poll_watermark == BAD.received_at

    extractor.answers["huge email"] = _extraction(ship_date=SHIP)
    clock.at = TODAY + EXTRACTION_RETRY_AFTER + timedelta(seconds=1)
    session.poll()

    assert _committed(durable, BAD.message_id)
    assert session.demonstration.last_poll_watermark == GOOD.received_at


def test_f_a_consumed_unacceptable_reply_does_not_pin_the_watermark(spy: QueueSpy) -> None:
    enquiry = _email(
        "<f-enq@c.example>", "Rate required", "old enquiry", TODAY - timedelta(days=1, hours=1)
    )
    reply = _email(
        "<f-reply@c.example>",
        "Re: Rate required",
        "thanks!",
        TODAY - timedelta(minutes=30),
        in_reply_to=enquiry.message_id,
    )
    durable = InMemoryStore()
    _restored(durable, "R-F", RequestState.QUOTATION_SENT, enquiry)
    session = _session(
        _ops_browser(),
        source=StubSource(enquiry, reply),
        extractor=ProviderExtractor({}),
        durable=durable,
    )

    session.poll()

    assert session.demonstration.last_poll_watermark == reply.received_at


# --- G. the production loop, replayed ------------------------------------------------------


def test_g_the_two_reply_loop_no_longer_calls_the_model_on_every_poll(spy: QueueSpy) -> None:
    """2026-09-23: one reply to a VALIDATED request (ff50…) and one to a quoted
    request (960c…), both restored after a restart, were extracted on every poll
    for ~47 minutes. Replayed over 30 polls: zero model calls for them, each
    reply recorded exactly once, and a new enquiry behind them still processed."""
    enq_v = _email(
        "<g-v@c.example>",
        "Rate required - Mumbai to Singapore",
        "enq v",
        TODAY - timedelta(hours=5),
    )
    enq_q = _email(
        "<g-q@c.example>",
        "Rate required - Bengaluru to Dubai",
        "enq q",
        TODAY - timedelta(hours=5, minutes=5),
    )
    reply_v = _email(
        "<g-rv@c.example>",
        "Re: Rate required - Mumbai to Singapore",
        "reply to validated",
        TODAY - timedelta(hours=2),
        in_reply_to=enq_v.message_id,
    )
    reply_q = _email(
        "<g-rq@c.example>",
        "Re: Rate required - Bengaluru to Dubai",
        "reply to quoted",
        TODAY - timedelta(hours=2),
        in_reply_to=enq_q.message_id,
    )
    new = _email(
        "<g-new@c.example>",
        "Rate required - Chennai to Singapore",
        "new enquiry",
        TODAY - timedelta(hours=1),
    )
    durable = InMemoryStore()
    _restored(durable, "R-V", RequestState.VALIDATED, enq_v)
    _restored(durable, "R-Q", RequestState.QUOTATION_SENT, enq_q)
    extractor = ProviderExtractor({"new enquiry": _extraction(ship_date=SHIP)})
    session = _session(
        _ops_browser(),
        source=StubSource(enq_v, enq_q, reply_v, reply_q, new),
        extractor=extractor,
        durable=durable,
    )

    for _ in range(30):
        session.poll()

    assert extractor.calls == ["new enquiry"], "one paid call in 30 polls, for the new enquiry"
    for request_id, reply in (("R-V", reply_v), ("R-Q", reply_q)):
        assert _committed(durable, reply.message_id)
        assert _events_for(session, request_id).count(AuditEventType.REPLY_NOT_ACCEPTED) == 1
    assert len(session.requests) == 3
    assert session.last_poll_error is None
