"""An unresolvable airport becomes a client clarification, not a dead end.

In browser mode a request whose origin or destination cannot be resolved to an
airport without guessing used to fail worker-side and sit as a `rate_failure`
nobody could act on. It now becomes a held clarification, drafted before any
enqueue, released only by the existing human approval, and answered by a reply
that merges as a change rather than a conflict.

Everything real runs — the router, the workflow, the validator, the state
machine, the resolver — with only the Redis queue replaced by a spy.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from tests.unit.test_live_browser_bridge import QueueSpy, _install_queue, _settings
from tests.unit.test_live_rate_failure import _complete
from tests.unit.test_web_live import APPROVER

from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.routing import CanonicalLocationResolver, StatedLocationResolver
from translog_quote.config import Settings, WebCargoMode
from translog_quote.domain.clarification import UnresolvedReason, location_question
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.shipment import (
    CargoDimensions,
    FieldName,
    RequestSource,
    ShipmentRecord,
)
from translog_quote.domain.validation import validate_shipment
from translog_quote.domain.workflow import TRANSITIONS, QuotationRequest, RequestState
from translog_quote.interface.jobs import JobState, JobStatus
from translog_quote.interface.web.live_session import LiveRequest, LiveSession
from translog_quote.pipeline.audit import AuditEventType
from translog_quote.pipeline.clarification_loop import DEFAULT_MAX_ROUNDS

DIMS = CargoDimensions(length=34, width=24, height=6)


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[AuditEventType] = []

    def record(self, event: object) -> None:
        self.events.append(event.event)  # type: ignore[attr-defined]


def _email(msg_id: str, subject: str, minutes: int, *, in_reply_to: str | None = None) -> RawEmail:
    return RawEmail(
        message_id=msg_id,
        from_address="client@example.com",
        subject=subject,
        body_text="See details.",
        received_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC) + timedelta(minutes=minutes),
        in_reply_to=in_reply_to,
    )


class _Source:
    """A source whose visible messages grow between polls."""

    def __init__(self, *states: tuple[RawEmail, ...]) -> None:
        self._states = list(states)
        self._last: tuple[RawEmail, ...] = ()

    def fetch_new(self) -> tuple[RawEmail, ...]:
        if self._states:
            self._last = self._states.pop(0)
        return self._last


class _ScriptedExtractor:
    def __init__(self, *results: ExtractionResult) -> None:
        self._results = list(results)

    def extract_shipment(self, _text: str) -> ExtractionResult:
        return self._results.pop(0)


def _session(
    settings: Settings,
    *,
    source: _Source,
    extractor: _ScriptedExtractor,
    sink: CollectingEmailSink,
    resolver: object | None = None,
    audit: _RecordingAudit | None = None,
) -> LiveSession:
    return LiveSession(
        settings,
        source=source,  # type: ignore[arg-type]
        sink=sink,
        extractor=extractor,  # type: ignore[arg-type]
        resolver=resolver,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
    )


@pytest.fixture
def browser() -> Settings:
    return _settings(WebCargoMode.BROWSER)


def _held(session: LiveSession) -> LiveRequest:
    held = [r for r in session.requests.values() if r.clarification is not None]
    assert len(held) == 1, f"expected one held clarification, got {len(held)}"
    return held[0]


# --- domain -----------------------------------------------------------------------


def test_transitions_allow_validated_to_needs_info() -> None:
    assert RequestState.NEEDS_INFO in TRANSITIONS[RequestState.VALIDATED]


def test_location_question_names_the_place_without_internal_vocabulary() -> None:
    text = location_question(FieldName.DESTINATION, "Dubai, UAE")
    assert "Dubai, UAE" in text
    lowered = text.lower()
    for internal in ("unresolvedlocation", "resolver", "canonical", "rule", "traceback"):
        assert internal not in lowered


def test_location_question_example_is_neutral_not_the_asked_place() -> None:
    """The format example is fixed and neutral: a question about Nairobi must not
    name Dubai (or any other unrelated place) as if it were the answer."""
    text = location_question(FieldName.ORIGIN, "Nairobi")
    assert "Nairobi" in text
    assert "Dubai" not in text


# --- Change 1 + 2: draft instead of enqueue -------------------------------------


def test_dubai_becomes_a_clarification_not_an_enqueue(
    browser: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    session = _session(
        browser,
        source=_Source((_email("<a@x>", "Delhi to Dubai", 0),)),
        extractor=_ScriptedExtractor(_complete("Delhi, India", "Dubai, UAE")),
        sink=CollectingEmailSink(),
    )

    session.poll()

    held = _held(session)
    assert held.state is RequestState.NEEDS_INFO
    assert held.rate_failure is None
    assert held.rate_job_id is None
    assert spy.enqueued == []
    (asked,) = held.clarification.unresolved  # type: ignore[union-attr]
    assert asked.field is FieldName.DESTINATION
    assert "Dubai, UAE" in asked.detail


def test_both_places_unresolved_make_one_draft_with_two_fields(
    browser: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    session = _session(
        browser,
        source=_Source((_email("<a@x>", "Atlantis to El Dorado", 0),)),
        extractor=_ScriptedExtractor(_complete("Atlantis", "El Dorado")),
        sink=CollectingEmailSink(),
    )

    session.poll()

    draft = _held(session).clarification
    assert draft is not None
    assert {u.field for u in draft.unresolved} == {FieldName.ORIGIN, FieldName.DESTINATION}
    assert all(u.reason is UnresolvedReason.AMBIGUOUS for u in draft.unresolved)
    assert spy.enqueued == []


def test_resolvable_places_still_enqueue(
    browser: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    session = _session(
        browser,
        source=_Source((_email("<a@x>", "Delhi to Singapore", 0),)),
        extractor=_ScriptedExtractor(_complete("Delhi, India", "Singapore")),
        sink=CollectingEmailSink(),
    )

    session.poll()

    assert [r for r in session.requests.values() if r.clarification is not None] == []
    assert len(spy.enqueued) == 1
    assert spy.enqueued[0].origin == "Delhi, India"  # stated wording preserved


@pytest.mark.parametrize("destination", ["Dubai (DXB)", "DXB"])
def test_explicit_codes_enqueue(
    browser: Settings, monkeypatch: pytest.MonkeyPatch, destination: str
) -> None:
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    session = _session(
        browser,
        source=_Source((_email("<a@x>", "Delhi to Dubai", 0),)),
        extractor=_ScriptedExtractor(_complete("Delhi (DEL)", destination)),
        sink=CollectingEmailSink(),
    )

    session.poll()

    assert [r for r in session.requests.values() if r.clarification is not None] == []
    assert len(spy.enqueued) == 1


def test_demo_mode_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """StatedLocationResolver never refuses a nameable place, so the same
    'Dubai, UAE' that clarifies in browser mode prices normally in demo mode."""
    session = _session(
        _settings(WebCargoMode.MOCK),
        source=_Source((_email("<a@x>", "Ahmedabad to Dubai", 0),)),
        extractor=_ScriptedExtractor(_complete("Ahmedabad", "Dubai, UAE")),
        sink=CollectingEmailSink(),
        resolver=StatedLocationResolver(),
    )

    session.poll()

    assert [r for r in session.requests.values() if r.clarification is not None] == []
    assert next(iter(session.requests.values())).state is RequestState.RATE_SELECTED


def test_polling_twice_drafts_once(
    browser: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    audit = _RecordingAudit()
    session = _session(
        browser,
        source=_Source((_email("<a@x>", "Delhi to Dubai", 0),)),
        extractor=_ScriptedExtractor(_complete("Delhi, India", "Dubai, UAE")),
        sink=CollectingEmailSink(),
        audit=audit,
    )

    session.poll()
    first = _held(session).clarification
    session.poll()

    assert _held(session).clarification is first
    assert audit.events.count(AuditEventType.LOCATION_UNRESOLVED) == 1
    assert spy.enqueued == []


# --- Change 5: an already-FAILED job is recovered -------------------------------


def _seed_validated(session: LiveSession, *, origin: str, destination: str) -> LiveRequest:
    """A validated request already carrying a (failed) job id — the shape a
    pre-existing enqueue leaves behind after this change ships."""
    record = ShipmentRecord(
        request_id="R-STUCK",
        source=RequestSource.EMAIL,
        origin=origin,
        destination=destination,
        weight_kg=500.0,
        dimensions_in=DIMS,
        commodity="Engineering components",
        cargo_type="Non-Haz",
        is_chemical=False,
        pcs=10,
        ship_date=date(2026, 9, 15),
    )
    stored = QuotationRequest(
        request_id="R-STUCK",
        state=RequestState.VALIDATED,
        record=record,
        client_address="client@example.com",
    )
    session._working.save_request(stored)  # type: ignore[attr-defined]
    request = LiveRequest(
        request_id="R-STUCK",
        client_address="client@example.com",
        state=RequestState.VALIDATED,
        record=record,
        validation=validate_shipment(record),
        last_message_id="<enq@x>",
        rate_job_id="job-1",
        messages=["<enq@x>"],
    )
    session.requests["R-STUCK"] = request
    session._demonstration.include("R-STUCK")  # type: ignore[attr-defined]
    return request


def test_a_failed_job_from_an_unresolved_place_becomes_a_draft(
    browser: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy(
        fetch_returns=JobStatus(
            job_id="job-1",
            state=JobState.FAILED,
            result=None,
            error="translog_quote.errors.taxonomy.UnresolvedLocation: 'Dubai, UAE' ...",
        )
    )
    _install_queue(monkeypatch, spy)
    session = _session(
        browser, source=_Source(()), extractor=_ScriptedExtractor(), sink=CollectingEmailSink()
    )
    request = _seed_validated(session, origin="Delhi, India", destination="Dubai, UAE")

    session.poll()

    assert request.clarification is not None
    assert request.state is RequestState.NEEDS_INFO
    assert request.rate_job_id is None  # the failed job is dropped
    assert request.rate_failure is None


def test_a_failed_job_that_still_resolves_keeps_the_rate_failure(
    browser: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A WebCargo autocomplete miss on a place that DID resolve to a code is a
    genuine search failure, not a client clarification — it must not email."""
    spy = QueueSpy(
        fetch_returns=JobStatus(
            job_id="job-1",
            state=JobState.FAILED,
            result=None,
            error="translog_quote.errors.taxonomy.UnresolvedLocation: WebCargo offered no match",
        )
    )
    _install_queue(monkeypatch, spy)
    session = _session(
        browser, source=_Source(()), extractor=_ScriptedExtractor(), sink=CollectingEmailSink()
    )
    # Both places resolve ("Dubai (DXB)" -> DXB); the failure is a WebCargo
    # autocomplete miss, so re-resolving succeeds and no clarification is drafted.
    request = _seed_validated(session, origin="Delhi (DEL)", destination="Dubai (DXB)")

    session.poll()

    assert request.clarification is None
    assert request.rate_failure is not None
    assert request.state is RequestState.VALIDATED


# --- Change 4 + adjustments: reply, hold, restart -------------------------------


def _reply(destination: str) -> ExtractionResult:
    return ExtractionResult(destination=ExtractedValue[str].stated(destination))


def test_a_reply_before_approval_is_held(
    browser: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    enquiry = _email("<enq@x>", "Delhi to Dubai", 0)
    reply = _email("<rep@x>", "Re: Delhi to Dubai", 5, in_reply_to="<enq@x>")
    session = _session(
        browser,
        source=_Source((enquiry,), (enquiry, reply)),
        extractor=_ScriptedExtractor(
            _complete("Delhi, India", "Dubai, UAE"), _reply("Dubai (DXB)")
        ),
        sink=CollectingEmailSink(),
    )

    session.poll()  # drafts, awaiting approval
    session.poll()  # the reply arrives, but no approval has happened yet

    held = _held(session)
    assert held.state is RequestState.NEEDS_INFO  # not advanced by the held reply
    assert held.record.destination is None  # still cleared; the reply was not merged
    assert spy.enqueued == []


def test_a_reply_replaces_the_place_and_enqueues(
    browser: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    enquiry = _email("<enq@x>", "Delhi to Dubai", 0)
    reply = _email("<rep@x>", "Re: Delhi to Dubai", 5, in_reply_to="<enq@x>")
    sink = CollectingEmailSink()
    session = _session(
        browser,
        source=_Source((enquiry,), (enquiry, reply)),
        extractor=_ScriptedExtractor(
            _complete("Delhi, India", "Dubai, UAE"), _reply("Dubai (DXB)")
        ),
        sink=sink,
    )

    session.poll()
    held = _held(session)
    assert sink.sent == []  # nothing before approval
    session.approve_clarification(by=APPROVER, request_id=held.request_id)
    assert len(sink.sent) == 1  # the clarification went out on approval
    session.poll()  # the reply merges into the cleared field

    request = session.requests[held.request_id]
    assert request.record.destination == "Dubai (DXB)"  # a change, not a conflict
    assert request.clarification is None
    assert len(spy.enqueued) == 1
    assert spy.enqueued[0].destination == "Dubai (DXB)"


def test_repeated_unresolvable_replies_escalate_to_manual_review(
    browser: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client who keeps answering with the same unusable wording is not asked
    forever: location drafts share the round cap, so after `_max_rounds` the
    existing futile-reply logic escalates to MANUAL_REVIEW and drafts no more."""
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    enquiry = _email("<enq@x>", "Delhi to Dubai", 0)
    replies = [
        _email(f"<rep{i}@x>", "Re: Delhi to Dubai", 10 * (i + 1), in_reply_to="<enq@x>")
        for i in range(DEFAULT_MAX_ROUNDS)
    ]
    # One cumulative snapshot per poll: enquiry alone, then one more reply each time.
    states = [(enquiry, *replies[:i]) for i in range(DEFAULT_MAX_ROUNDS + 1)]
    sink = CollectingEmailSink()
    session = _session(
        browser,
        source=_Source(*states),
        extractor=_ScriptedExtractor(
            _complete("Delhi, India", "Dubai, UAE"),
            *[_reply("Dubai") for _ in replies],  # still unresolvable, every time
        ),
        sink=sink,
    )

    session.poll()  # enquiry -> first location draft
    request_id = _held(session).request_id
    for _ in range(DEFAULT_MAX_ROUNDS):
        session.approve_clarification(by=APPROVER, request_id=request_id)
        session.poll()  # the reply re-drafts, until the cap escalates

    request = session.requests[request_id]
    assert request.state is RequestState.MANUAL_REVIEW
    assert len(sink.sent) == DEFAULT_MAX_ROUNDS  # one send per approved draft, no more
    assert [r for r in session.requests.values() if r.clarification is not None] == []
    assert spy.enqueued == []  # never searched under an unresolved place


def test_a_restart_before_approval_redrafts_once_and_durable_stays_clean(
    browser: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Change 4, option (b): the cleared field is never persisted before
    approval, so a restart re-derives the draft rather than stranding it."""
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    enquiry = _email("<enq@x>", "Delhi to Dubai", 0)
    session = _session(
        browser,
        source=_Source((enquiry,)),
        extractor=_ScriptedExtractor(_complete("Delhi, India", "Dubai, UAE")),
        sink=CollectingEmailSink(),
    )
    session.poll()
    request_id = _held(session).request_id

    # Durable still holds the ORIGINAL place, at VALIDATED — nothing was cleared.
    durable = session._durable.get_request(request_id)  # type: ignore[attr-defined]
    assert durable is not None
    assert durable.record.destination == "Dubai, UAE"
    assert durable.state is RequestState.VALIDATED

    # A restart: a fresh session seeded from the same durable store.
    reborn = LiveSession(
        browser,
        source=_Source(()),
        sink=CollectingEmailSink(),
        extractor=_ScriptedExtractor(),
        resolver=CanonicalLocationResolver(),
        durable=session._durable,  # type: ignore[attr-defined]
    )
    reborn.poll()  # re-derives the draft from the restored VALIDATED request

    restored = reborn.requests[request_id]
    assert restored.clarification is not None
    assert restored.state is RequestState.NEEDS_INFO
