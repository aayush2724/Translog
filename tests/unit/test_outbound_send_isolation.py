"""An automatic client email that cannot be sent no longer wedges the mailbox.

Two sends happen on their own, mid-turn, after the message was already
extracted (a paid model call) and before anything of the turn is persisted: the
extraction-failure notice and the non-answer reminder. A Gmail failure on either
used to escape `LiveSession.poll`. The message stayed unrecorded and sorted
first, so every poll re-extracted it (paid), failed again, and never reached the
newer mail, the rate searches or the watermark behind it — the same cost-drain
shape as the 2026-09-23 OpenRouter incident.

The send failure is now translated into `OutboundUnavailable` at those two send
sites and isolated to the one message, exactly like `ExtractionUnavailable`:
left unrecorded (the watermark holds at it) and held for `OUTBOUND_RETRY_AFTER`
so its extraction is not paid for again on every poll.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.unit.test_clarification_loop import (
    REQ,
    _followup_workflow,
    approve,
    complete,
    email,
    workflow,
)
from tests.unit.test_end_to_end_regressions import TODAY, _email, _extraction
from tests.unit.test_gmail_thread import StubSource
from tests.unit.test_live_browser_bridge import QueueSpy, _settings
from tests.unit.test_reply_gate_and_extraction_isolation import (
    SHIP,
    MovableClock,
    ProviderExtractor,
    _committed,
    _ops_browser,
    spy,  # noqa: F401 - pytest fixture
)

from translog_quote.adapters.store import InMemoryStore
from translog_quote.config import Settings, WebCargoMode
from translog_quote.domain.email import OutboundMessage
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.workflow import RequestState
from translog_quote.errors import ContractViolation, OutboundUnavailable, TransientFailure
from translog_quote.interface.web.live_session import OUTBOUND_RETRY_AFTER, LiveSession
from translog_quote.pipeline.audit import AuditEventType


class FlakySink:
    """A mail sink that raises what it is told to, until told otherwise."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.sent: list[OutboundMessage] = []
        self.attempts = 0

    def send(self, message: OutboundMessage) -> None:
        self.attempts += 1
        if self.fail is not None:
            raise self.fail
        self.sent.append(message)


#: The model's answer could not be read — the path that sends the client the
#: automatic "we could not process your message" notice.
UNREADABLE = ContractViolation("model output did not match the extraction contract")
GMAIL_TIMEOUT = TransientFailure("Gmail send timed out after 30s")

BAD = _email(
    "<o-bad@c.example>",
    "Rate required - scanned form",
    "unreadable email",
    TODAY - timedelta(hours=3),
)
GOOD = _email(
    "<o-good@c.example>",
    "Rate required - Chennai to Singapore",
    "short enquiry",
    TODAY - timedelta(hours=1),
)


def _session(
    settings: Settings,
    *,
    sink: FlakySink,
    extractor: ProviderExtractor,
    source: object,
    durable: InMemoryStore | None = None,
    clock: MovableClock | None = None,
) -> LiveSession:
    return LiveSession(
        settings,
        source=source,  # type: ignore[arg-type]
        sink=sink,  # type: ignore[arg-type]
        extractor=extractor,  # type: ignore[arg-type]
        durable=durable,
        clock=clock or MovableClock(TODAY),
    )


def _outbound_events(session: LiveSession) -> list[dict[str, object]]:
    return [
        dict(e.detail)
        for e in session.audit.events
        if e.event is AuditEventType.OUTBOUND_UNAVAILABLE
    ]


def test_a_failed_notice_send_does_not_abort_the_poll(spy: QueueSpy) -> None:  # noqa: F811
    extractor = ProviderExtractor(
        {"unreadable email": UNREADABLE, "short enquiry": _extraction(ship_date=SHIP)}
    )
    durable = InMemoryStore()
    session = _session(
        _settings(WebCargoMode.BROWSER),
        sink=FlakySink(fail=GMAIL_TIMEOUT),
        extractor=extractor,
        source=StubSource(BAD, GOOD),
        durable=durable,
    )

    session.poll()  # used to raise TransientFailure here

    # The older message failed first; the newer one was still processed, all
    # the way to its rate search.
    assert extractor.calls == ["unreadable email", "short enquiry"]
    [request] = session.requests.values()
    assert request.enquiry is not None
    assert request.enquiry.message_id == GOOD.message_id
    assert request.state is RequestState.VALIDATED
    assert len(spy.enqueued) == 1
    # The failed message is neither consumed nor a request, and it is reported.
    assert not _committed(durable, BAD.message_id)
    assert not session._seen_message(BAD.message_id)
    assert session.last_poll_error == "OutboundUnavailable"
    assert _outbound_events(session) == [{"error": "TransientFailure", "permanent": False}]


def test_a_failed_send_is_held_so_its_paid_extraction_is_not_repeated_every_poll(
    spy: QueueSpy,  # noqa: F811
) -> None:
    clock = MovableClock(TODAY)
    extractor = ProviderExtractor({"unreadable email": UNREADABLE})
    sink = FlakySink(fail=GMAIL_TIMEOUT)
    durable = InMemoryStore()
    session = _session(
        _settings(WebCargoMode.BROWSER),
        sink=sink,
        extractor=extractor,
        source=StubSource(BAD),
        durable=durable,
        clock=clock,
    )

    session.poll()
    for _ in range(5):  # a poll every few seconds
        clock.at += timedelta(seconds=15)
        session.poll()

    assert extractor.calls == ["unreadable email"], "one paid call, not one per poll"
    assert sink.attempts == 1
    assert session.last_poll_error == "OutboundUnavailable", "still reported while held"

    # Gmail recovers; after the hold the message is handled once more, the
    # notice goes out, and the request is handed to a person as designed.
    sink.fail = None
    clock.at = TODAY + OUTBOUND_RETRY_AFTER + timedelta(seconds=1)
    session.poll()

    assert extractor.calls == ["unreadable email", "unreadable email"]
    assert len(sink.sent) == 1, "exactly one failure notice"
    assert sink.sent[0].to_address == BAD.from_address
    [request] = session.requests.values()
    assert request.state is RequestState.MANUAL_REVIEW
    assert _committed(durable, BAD.message_id)
    assert session.last_poll_error is None, "recovered, and says so"


def test_a_permanent_send_rejection_is_isolated_and_held_too(spy: QueueSpy) -> None:  # noqa: F811
    extractor = ProviderExtractor({"unreadable email": UNREADABLE})
    session = _session(
        _settings(WebCargoMode.BROWSER),
        sink=FlakySink(fail=ContractViolation("Gmail rejected the send (400): Invalid To header")),
        extractor=extractor,
        source=StubSource(BAD),
    )

    session.poll()
    session.poll()

    assert extractor.calls == ["unreadable email"]
    assert session.requests == {}
    assert _outbound_events(session) == [{"error": "ContractViolation", "permanent": True}]


def test_the_watermark_holds_at_a_failed_send_and_advances_once_it_is_handled(
    spy: QueueSpy,  # noqa: F811
) -> None:
    clock = MovableClock(TODAY)
    extractor = ProviderExtractor(
        {"unreadable email": UNREADABLE, "short enquiry": _extraction(ship_date=SHIP)}
    )
    sink = FlakySink(fail=GMAIL_TIMEOUT)
    durable = InMemoryStore()
    session = _session(
        _ops_browser(),
        sink=sink,
        extractor=extractor,
        source=StubSource(BAD, GOOD),
        durable=durable,
        clock=clock,
    )

    session.poll()

    # GOOD (newer) is committed; BAD (older) is not, so the cutoff holds at BAD
    # rather than jumping past it — a restart would still re-read it.
    assert _committed(durable, GOOD.message_id)
    assert session.demonstration.last_poll_watermark == BAD.received_at

    sink.fail = None
    clock.at = TODAY + OUTBOUND_RETRY_AFTER + timedelta(seconds=1)
    session.poll()

    assert _committed(durable, BAD.message_id)
    assert session.demonstration.last_poll_watermark == GOOD.received_at


def test_an_unrelated_sink_error_is_not_swallowed(spy: QueueSpy) -> None:  # noqa: F811
    """Only the provider's failure taxonomy is isolated; a real bug still escapes."""
    extractor = ProviderExtractor({"unreadable email": UNREADABLE})
    session = _session(
        _settings(WebCargoMode.BROWSER),
        sink=FlakySink(fail=RuntimeError("a real bug")),
        extractor=extractor,
        source=StubSource(BAD),
    )

    with pytest.raises(RuntimeError):
        session.poll()


# --- the reminder send, and the operator send, at the workflow -----------------------------


def test_a_failed_reminder_send_persists_nothing_and_a_retry_sends_exactly_one() -> None:
    initial = complete(pcs=ExtractedValue[int].not_stated())
    wf, _, store = _followup_workflow(initial, ExtractionResult(), ExtractionResult())
    wf.handle(REQ, email("no piece count", n=1))
    approve(wf)
    before = store.get_request(REQ)
    assert before is not None
    assert before.state is RequestState.CLARIFICATION_SENT

    flaky = FlakySink(fail=GMAIL_TIMEOUT)
    wf._sink = flaky  # type: ignore[assignment]
    non_answer = email("still checking, will send shortly", n=2)

    with pytest.raises(OutboundUnavailable) as caught:
        wf.handle(REQ, non_answer)

    assert caught.value.permanent is False
    assert store.get_request(REQ) == before, "nothing of the failed turn was persisted"

    flaky.fail = None
    outcome = wf.handle(REQ, non_answer)

    assert outcome.state is RequestState.CLARIFICATION_SENT
    assert len(flaky.sent) == 1, "exactly one reminder"
    assert "remain open for the next 30 minutes" in flaky.sent[0].body_text
    stored = store.get_request(REQ)
    assert stored is not None
    assert stored.clarification_followup_sent_at is not None


def test_an_operator_approved_send_failure_is_unchanged() -> None:
    """The approval send is not wrapped: its failure reaches the operator as before."""
    wf, _ = workflow(complete(pcs=ExtractedValue[int].not_stated()))
    assert wf.handle(REQ, email("no piece count", n=1)).state is RequestState.NEEDS_INFO
    wf._sink = FlakySink(fail=GMAIL_TIMEOUT)  # type: ignore[assignment]

    with pytest.raises(TransientFailure):
        approve(wf)
