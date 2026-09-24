"""A client who corrects a value to a new one can now actually correct it.

The defect: merge rule 3 keeps the stored value when a reply disagrees with it
and reports a conflict, and the loop drafts "we have received two different
values, 500 and 700 — which is correct?". But the stored record still held 500,
so the client's answer "700 is correct" disagreed with it again: conflict,
conflict, then MANUAL_REVIEW after the round budget ("After 3 clarification
rounds the shipment still needs: weight_kg"). Only re-confirming the *old* value
could ever converge.

Now, when a drafted clarification asks the client to choose, the stored record
leaves that field open (as the location clarification already does for the
place it asks about), so the answer fills it by merge rule 1. The merge rules,
the state machine and the persistence model are unchanged; a draft is still
never persisted durably, so a restart re-derives the same conflict question.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from tests.unit.test_clarification_loop import (
    REQ,
    _followup_workflow,
    _RecordingAudit,
    approve,
    complete,
    email,
    workflow,
)
from tests.unit.test_end_to_end_regressions import APPROVER, TODAY, _email, _extraction
from tests.unit.test_live_browser_bridge import QueueSpy
from tests.unit.test_reply_gate_and_extraction_isolation import (
    SHIP,
    ProviderExtractor,
    _ops_browser,
    _session,
    spy,  # noqa: F401 - pytest fixture
)

from translog_quote.adapters.store import InMemoryStore
from translog_quote.domain.clarification import UnresolvedReason
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.workflow import RequestState
from translog_quote.pipeline.audit import AuditEvent, AuditEventType


def _weight(kg: float) -> ExtractionResult:
    return ExtractionResult(weight_kg=ExtractedValue[float].stated(kg))


def _to_conflict(*later: ExtractionResult):  # type: ignore[no-untyped-def]
    """Enquiry at 500 kg with no piece count -> clarification sent -> a reply
    that gives the pieces but says 700 kg: the conflict question is drafted."""
    wf, sink = workflow(
        complete(pcs=ExtractedValue[int].not_stated()),
        ExtractionResult(
            pcs=ExtractedValue[int].stated(15), weight_kg=ExtractedValue[float].stated(700.0)
        ),
        *later,
    )
    wf.handle(REQ, email("500 kg, no piece count", n=1))
    approve(wf)
    conflict = wf.handle(REQ, email("15 pieces, and it is actually 700 kg", n=2))
    assert conflict.state is RequestState.NEEDS_INFO
    assert conflict.clarification is not None
    assert [u.reason for u in conflict.clarification.unresolved] == [UnresolvedReason.CONFLICT]
    return wf, sink, conflict


def test_answering_the_conflict_with_the_new_value_converges_to_it() -> None:
    wf, _, _ = _to_conflict(_weight(700.0))
    approve(wf)

    answer = wf.handle(REQ, email("700 kg is correct", n=3))

    assert not answer.merge.has_conflicts, "the answer no longer conflicts with the old value"
    assert answer.record.weight_kg == 700.0
    assert answer.state is RequestState.VALIDATED
    assert answer.clarification is None
    assert answer.round_number == 2, "converged, not escalated after the round budget"


def test_answering_the_conflict_with_the_old_value_still_converges() -> None:
    wf, _, _ = _to_conflict(_weight(500.0))
    approve(wf)

    answer = wf.handle(REQ, email("sorry, 500 kg was right", n=3))

    assert answer.record.weight_kg == 500.0
    assert answer.state is RequestState.VALIDATED


def test_a_conflict_is_still_never_silently_resolved_when_the_draft_is_made() -> None:
    """Rule 3 is untouched: the reply that disagrees does not overwrite, and the
    question still names both values."""
    _, _, conflict = _to_conflict()

    assert conflict.merge.has_conflicts
    assert conflict.record.weight_kg == 500.0, "the outcome still shows the value in question"
    body = conflict.clarification.body_text
    assert "500" in body and "700" in body


def test_only_the_field_asked_about_is_left_open() -> None:
    wf, _, _ = _to_conflict()

    stored = wf._store.get_request(REQ)
    assert stored is not None
    assert stored.state is RequestState.NEEDS_INFO
    assert stored.record.weight_kg is None, "open for the client's answer"
    assert stored.record.pcs == 15, "the reply's new, agreeing detail is kept"
    assert stored.record.origin == "Ahmedabad", "everything else is untouched"


def test_an_answer_that_skips_the_conflicted_field_asks_for_it_not_a_guess() -> None:
    wf, _, _ = _to_conflict(
        ExtractionResult(commodity=ExtractedValue[str].stated("Industrial adhesive"))
    )
    approve(wf)

    answer = wf.handle(REQ, email("the commodity is industrial adhesive", n=3))

    assert answer.record.weight_kg is None, "neither value is picked for the client"
    assert answer.state is RequestState.NEEDS_INFO
    assert answer.clarification is not None
    assert [(u.field.value, u.reason) for u in answer.clarification.unresolved] == [
        ("weight_kg", UnresolvedReason.MISSING)
    ]


def _conflict_events(events: list[AuditEvent]) -> list[dict[str, Any]]:
    return [dict(e.detail) for e in events if e.event is AuditEventType.CONFLICT_DETECTED]


def test_the_audit_keeps_the_conflicting_field_and_both_values() -> None:
    """The stored record leaves the field open while the question is out, so the
    audit is where the original disagreement is kept — structured, not prose."""
    audit = _RecordingAudit()
    wf, _, store = _followup_workflow(
        complete(pcs=ExtractedValue[int].not_stated()),
        ExtractionResult(
            pcs=ExtractedValue[int].stated(15), weight_kg=ExtractedValue[float].stated(700.0)
        ),
        _weight(700.0),
        audit=audit,
    )
    wf.handle(REQ, email("500 kg, no piece count", n=1))
    approve(wf)
    wf.handle(REQ, email("15 pieces, and it is actually 700 kg", n=2))

    stored = store.get_request(REQ)
    assert stored is not None
    assert stored.record.weight_kg is None, "the field is open in the stored record"
    assert _conflict_events(audit.events) == [
        {
            "fields": ["weight_kg"],
            "conflicts": [{"field": "weight_kg", "existing_value": 500.0, "incoming_value": 700.0}],
        }
    ]

    # The evidence is unaffected by the successful correction that follows.
    approve(wf)
    answer = wf.handle(REQ, email("700 kg is correct", n=3))
    assert answer.record.weight_kg == 700.0
    assert answer.state is RequestState.VALIDATED
    assert len(_conflict_events(audit.events)) == 1, "the answer itself is not a conflict"


def test_non_numeric_conflict_values_are_recorded_as_plain_json() -> None:
    audit = _RecordingAudit()
    wf, _, _ = _followup_workflow(
        complete(pcs=ExtractedValue[int].not_stated()),  # to Bahrain, ships 2026-09-15
        ExtractionResult(
            pcs=ExtractedValue[int].stated(15),
            destination=ExtractedValue[str].stated("Dubai"),
            ship_date=ExtractedValue[date].stated(date(2026, 9, 20)),
        ),
        audit=audit,
    )
    wf.handle(REQ, email("to Bahrain on 15 September, no piece count", n=1))
    approve(wf)
    wf.handle(REQ, email("15 pcs, to Dubai on 20 September", n=2))

    [event] = _conflict_events(audit.events)
    recorded = {c["field"]: (c["existing_value"], c["incoming_value"]) for c in event["conflicts"]}
    assert recorded == {
        "destination": ("Bahrain", "Dubai"),
        "ship_date": ("2026-09-15", "2026-09-20"),
    }


def test_two_conflicts_asked_together_both_converge() -> None:
    wf, _ = workflow(
        complete(pcs=ExtractedValue[int].not_stated()),
        ExtractionResult(
            pcs=ExtractedValue[int].stated(15),
            weight_kg=ExtractedValue[float].stated(700.0),
            destination=ExtractedValue[str].stated("Dubai"),
        ),
        ExtractionResult(
            weight_kg=ExtractedValue[float].stated(700.0),
            destination=ExtractedValue[str].stated("Dubai"),
        ),
    )
    wf.handle(REQ, email("500 kg to Bahrain, no piece count", n=1))
    approve(wf)
    conflict = wf.handle(REQ, email("15 pcs, 700 kg, to Dubai", n=2))
    assert conflict.clarification is not None
    assert {u.field.value for u in conflict.clarification.unresolved} == {
        "weight_kg",
        "destination",
    }
    approve(wf)

    answer = wf.handle(REQ, email("700 kg and Dubai are correct", n=3))

    assert answer.record.weight_kg == 700.0
    assert answer.record.destination == "Dubai"
    assert answer.state is RequestState.VALIDATED


# --- through the live session, across a restart --------------------------------------------


class Inbox:
    """A mailbox a test adds messages to between polls."""

    def __init__(self, *emails: RawEmail) -> None:
        self.emails = list(emails)

    def fetch_new(self, *, since: object = None) -> tuple[RawEmail, ...]:
        return tuple(self.emails)


ENQ = _email("<cc-enq@c.example>", "Rate required", "enquiry", TODAY - timedelta(hours=3))
CORRECTION = _email(
    "<cc-r1@c.example>",
    "Re: Rate required",
    "10 pieces, and it is actually 700 kg",
    TODAY - timedelta(hours=2),
    in_reply_to=ENQ.message_id,
)
ANSWER = _email(
    "<cc-r2@c.example>",
    "Re: Rate required",
    "700 kg is correct",
    TODAY - timedelta(hours=1),
    in_reply_to=CORRECTION.message_id,
)


def test_live_a_restart_before_the_conflict_question_is_sent_re_asks_it(
    spy: QueueSpy,  # noqa: F811
) -> None:
    """The cleared field lives only in the working store while the draft awaits
    approval. A restart there re-derives the same conflict from the durable
    record (still 500) — it never silently accepts the disputed 700."""
    settings = _ops_browser()
    durable = InMemoryStore()
    answers: dict[str, ExtractionResult | Exception] = {
        "enquiry": _extraction(pcs=ExtractedValue[int].not_stated(), ship_date=SHIP),
        "10 pieces, and it is actually 700 kg": ExtractionResult(
            pcs=ExtractedValue[int].stated(10), weight_kg=ExtractedValue[float].stated(700.0)
        ),
        "700 kg is correct": _weight(700.0),
    }
    inbox = Inbox(ENQ)
    first = _session(settings, source=inbox, extractor=ProviderExtractor(answers), durable=durable)
    first.poll()
    [rid] = first.requests
    first.approve_clarification(by=APPROVER, request_id=rid)
    inbox.emails.append(CORRECTION)
    first.poll()
    assert first.requests[rid].state is RequestState.NEEDS_INFO
    durable_record = durable.get_request(rid)
    assert durable_record is not None
    assert durable_record.record.weight_kg == 500.0, "the unsent draft was not committed"

    # Restart with the conflict question still unsent.
    second = _session(settings, source=inbox, extractor=ProviderExtractor(answers), durable=durable)
    second.poll()
    request = second.requests[rid]
    assert request.state is RequestState.NEEDS_INFO
    assert request.clarification is not None
    assert [u.reason for u in request.clarification.unresolved] == [UnresolvedReason.CONFLICT]

    # The question goes out, the client answers with the new value, and it sticks.
    second.approve_clarification(by=APPROVER, request_id=rid)
    inbox.emails.append(ANSWER)
    second.poll()

    request = second.requests[rid]
    assert request.state is RequestState.VALIDATED
    assert request.record.weight_kg == 700.0
    stored = durable.get_request(rid)
    assert stored is not None
    assert stored.record.weight_kg == 700.0
    assert len(spy.enqueued) == 1, "the corrected shipment reaches the rate search"


def test_live_the_conflict_evidence_survives_a_restart_after_the_field_is_cleared(
    spy: QueueSpy,  # noqa: F811
) -> None:
    """Once the conflict question is approved, the durable record holds no
    weight at all; the persisted audit trail still says 500 versus 700."""
    settings = _ops_browser()
    durable = InMemoryStore()
    answers: dict[str, ExtractionResult | Exception] = {
        "enquiry": _extraction(pcs=ExtractedValue[int].not_stated(), ship_date=SHIP),
        "10 pieces, and it is actually 700 kg": ExtractionResult(
            pcs=ExtractedValue[int].stated(10), weight_kg=ExtractedValue[float].stated(700.0)
        ),
    }
    inbox = Inbox(ENQ)
    first = _session(settings, source=inbox, extractor=ProviderExtractor(answers), durable=durable)
    first.poll()
    [rid] = first.requests
    first.approve_clarification(by=APPROVER, request_id=rid)
    inbox.emails.append(CORRECTION)
    first.poll()
    first.approve_clarification(by=APPROVER, request_id=rid)  # the conflict question
    stored = durable.get_request(rid)
    assert stored is not None
    assert stored.state is RequestState.CLARIFICATION_SENT
    assert stored.record.weight_kg is None, "cleared durably once the question went out"

    # Restart while the client has not answered yet.
    second = _session(settings, source=inbox, extractor=ProviderExtractor(answers), durable=durable)

    conflicts = [
        c
        for e in second.audit.events
        if e.request_id == rid and e.event is AuditEventType.CONFLICT_DETECTED
        for c in e.detail["conflicts"]
    ]
    assert conflicts == [{"field": "weight_kg", "existing_value": 500.0, "incoming_value": 700.0}]
