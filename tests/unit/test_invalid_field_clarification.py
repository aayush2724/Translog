"""Field-level invalid data is clarified on the same thread, never escalated.

A client who states an out-of-range value (-5 pieces, 0 kg, a 0 dimension) has
given something understandable and correctable. The extraction marks it INVALID
(the openrouter adapter does this before the contract runs), and from there the
*existing* ClarificationWorkflow asks for a valid value, merges the correction,
re-validates and continues — exactly like a missing field. A genuinely malformed
extraction still hands over to a person via the ContractViolation path.
"""

from __future__ import annotations

from tests.unit.test_clarification_loop import (
    REQ,
    ScriptedExtractor,
    _RaisingExtractor,
    _RecordingAudit,
    approve,
    complete,
    email,
    workflow,
)
from tests.unit.test_inbound_router import enquiry, reply
from tests.unit.test_ingestion_filtering import _router

from translog_quote.adapters.clock import FixedClock
from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.store import InMemoryStore
from translog_quote.domain.clarification import UnresolvedReason
from translog_quote.domain.clarification.questions import invalid_question
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.shipment import CargoDimensions, FieldName
from translog_quote.domain.workflow import RequestState
from translog_quote.pipeline import ClarificationWorkflow
from translog_quote.pipeline.audit import AuditEventType

INVALID_PCS = ExtractedValue[int].invalid(note="stated -5")
INVALID_WEIGHT = ExtractedValue[float].invalid(note="stated 0")
INVALID_DIMS = ExtractedValue[CargoDimensions].invalid(note="stated 0 x 30 x 30")


# 1. Negative pieces → clarification → valid pieces reply → validation continues.
def test_invalid_pieces_clarifies_then_a_valid_reply_continues() -> None:
    initial = complete(pcs=INVALID_PCS)
    reply = ExtractionResult(pcs=ExtractedValue[int].stated(15))
    wf, _sink = workflow(initial, reply)

    first = wf.handle(REQ, email("pieces are -5", n=1))
    assert first.awaiting_approval
    assert first.clarification is not None
    assert first.clarification.asked_for == (FieldName.PCS,)
    assert UnresolvedReason.INVALID in first.clarification.reasons
    assert invalid_question(FieldName.PCS) in first.clarification.body_text

    approve(wf)
    second = wf.handle(REQ, email("15 pieces", n=2))
    assert second.is_complete
    assert second.state is RequestState.VALIDATED


# 2. Zero/negative weight → clarification → valid weight reply → continues.
def test_invalid_weight_clarifies_then_a_valid_reply_continues() -> None:
    initial = complete(weight_kg=INVALID_WEIGHT)
    reply = ExtractionResult(weight_kg=ExtractedValue[float].stated(500.0))
    wf, _sink = workflow(initial, reply)

    first = wf.handle(REQ, email("0 kg", n=1))
    assert first.clarification is not None
    assert first.clarification.asked_for == (FieldName.WEIGHT_KG,)
    assert "greater than 0 kg" in first.clarification.body_text

    approve(wf)
    assert wf.handle(REQ, email("500 kg", n=2)).is_complete


# 3. Zero/negative dimensions → clarification → valid dimensions reply → continues.
def test_invalid_dimensions_clarify_then_a_valid_reply_continues() -> None:
    initial = complete(dimensions_in=INVALID_DIMS)
    reply = ExtractionResult(
        dimensions_in=ExtractedValue[CargoDimensions].stated(
            CargoDimensions(length=40, width=30, height=30)
        )
    )
    wf, _sink = workflow(initial, reply)

    first = wf.handle(REQ, email("0 x 30 x 30", n=1))
    assert first.clarification is not None
    assert first.clarification.asked_for == (FieldName.DIMENSIONS_IN,)
    assert "Each dimension must be greater than 0" in first.clarification.body_text

    approve(wf)
    assert wf.handle(REQ, email("40 x 30 x 30", n=2)).is_complete


# 4. Multiple invalid fields → ONE clarification containing all of them.
def test_multiple_invalid_fields_produce_one_clarification() -> None:
    initial = complete(pcs=INVALID_PCS, weight_kg=INVALID_WEIGHT, dimensions_in=INVALID_DIMS)
    wf, sink = workflow(initial)

    outcome = wf.handle(REQ, email("everything invalid"))

    assert outcome.clarification is not None
    assert set(outcome.clarification.asked_for) == {
        FieldName.PCS,
        FieldName.WEIGHT_KG,
        FieldName.DIMENSIONS_IN,
    }
    assert outcome.clarification.reasons == frozenset({UnresolvedReason.INVALID})
    assert sink.sent == [], "one draft, held for approval — nothing sent yet"


# 5. Invalid reply followed by another invalid reply → clarification stays active.
def test_a_second_invalid_reply_keeps_clarifying_not_manual_review() -> None:
    initial = complete(pcs=INVALID_PCS)
    reply_still_invalid = ExtractionResult(pcs=ExtractedValue[int].invalid(note="stated -3"))
    wf, _sink = workflow(initial, reply_still_invalid)

    wf.handle(REQ, email("pieces -5", n=1))
    approve(wf)
    second = wf.handle(REQ, email("pieces -3", n=2))

    assert second.state is RequestState.NEEDS_INFO, "re-clarified, not escalated"
    assert not second.needs_a_person
    assert second.clarification is not None
    assert second.clarification.asked_for == (FieldName.PCS,)


# 6. Invalid reply followed by a valid reply → workflow continues.
def test_invalid_then_valid_reply_reaches_validated() -> None:
    initial = complete(pcs=INVALID_PCS)
    reply = ExtractionResult(pcs=ExtractedValue[int].stated(9))
    wf, _sink = workflow(initial, reply)
    wf.handle(REQ, email("bad", n=1))
    approve(wf)
    out = wf.handle(REQ, email("9 pieces", n=2))
    assert out.state is RequestState.VALIDATED
    assert out.record.pcs == 9


# 7 & 12. Invalid-data clarification survives restart; no duplicate clarification after.
def test_invalid_clarification_survives_restart_and_a_valid_reply_continues() -> None:
    store = InMemoryStore()
    sink = CollectingEmailSink()
    wf = ClarificationWorkflow(
        extractor=ScriptedExtractor(complete(pcs=INVALID_PCS)),
        sink=sink,
        store=store,
        clock=FixedClock(),
    )
    wf.handle(REQ, email("pieces -5", n=1))
    approve(wf)  # CLARIFICATION_SENT, persisted to the store
    sent_before = len(sink.sent)

    # Restart: a fresh workflow over the same store, fresh in-memory rounds/pending.
    reborn_sink = CollectingEmailSink()
    reborn = ClarificationWorkflow(
        extractor=ScriptedExtractor(ExtractionResult(pcs=ExtractedValue[int].stated(15))),
        sink=reborn_sink,
        store=store,
        clock=FixedClock(),
    )
    resumed = reborn.handle(REQ, email("15 pieces", n=2))

    assert resumed.is_complete, "the corrected reply merged into the restored request"
    assert resumed.state is RequestState.VALIDATED
    assert len(sink.sent) == sent_before, "no duplicate clarification after restart"
    assert reborn_sink.sent == [], "the reply resolved it; nothing re-sent"


# 10. A genuinely malformed extraction still follows the MANUAL_REVIEW path.
def test_a_genuine_contract_violation_still_goes_to_manual_review() -> None:
    audit = _RecordingAudit()
    sink = CollectingEmailSink()
    wf = ClarificationWorkflow(
        extractor=_RaisingExtractor(),  # raises ContractViolation
        sink=sink,
        store=InMemoryStore(),
        clock=FixedClock(),
        audit=audit,
    )

    outcome = wf.handle(REQ, email("garbled model output"))

    assert outcome.state is RequestState.MANUAL_REVIEW
    assert outcome.needs_a_person
    reasons = [
        e.detail.get("reason")
        for e in audit.events
        if e.event is AuditEventType.MANUAL_REVIEW_ESCALATED
    ]
    assert "extraction_contract_violation" in reasons


# 11. No failure notice is sent for field-level invalid data.
def test_no_failure_notice_is_sent_for_invalid_field_data() -> None:
    initial = complete(pcs=INVALID_PCS)
    wf, sink = workflow(initial)

    wf.handle(REQ, email("pieces -5"))
    approve(wf)  # releases the clarification

    assert len(sink.sent) == 1
    body = sink.sent[0].body_text  # type: ignore[attr-defined]
    assert "number of pieces must be greater than 0" in body
    assert "unable to process" not in body, "a clarification, not a failure notice"


# --- the recognition edge case: an enquiry whose ONLY shipment content is invalid --
#
# A first-contact enquiry that states shipment fields but every one is out of
# range (all INVALID, nothing STATED) must NOT be discarded as unrelated mail —
# it is a genuine enquiry that needs a clarification. INVALID counts as "the
# client stated a shipment field", so recognition keeps it; but INVALID is only
# ever produced for a field the email really stated, so a stray number in a
# receipt (which stays NOT_STATED) never becomes an enquiry.


def _all_invalid() -> ExtractionResult:
    """Only invalid shipment values — no STATED field at all."""
    return ExtractionResult(
        pcs=INVALID_PCS, weight_kg=INVALID_WEIGHT, dimensions_in=INVALID_DIMS
    )


def test_first_contact_with_only_invalid_pieces_is_an_enquiry_not_ignored() -> None:
    reply = ExtractionResult(pcs=ExtractedValue[int].stated(15))
    wf, _sink = workflow(ExtractionResult(pcs=INVALID_PCS), reply)

    first = wf.handle(REQ, email("just: pieces -5", n=1))

    assert first.is_non_enquiry is False, "not discarded — it is a genuine enquiry"
    assert first.clarification is not None
    assert FieldName.PCS in first.clarification.asked_for


def test_first_contact_with_only_invalid_weight_is_an_enquiry() -> None:
    wf, _sink = workflow(ExtractionResult(weight_kg=INVALID_WEIGHT))
    first = wf.handle(REQ, email("weight 0 kg"))
    assert first.is_non_enquiry is False
    assert first.clarification is not None and FieldName.WEIGHT_KG in first.clarification.asked_for


def test_first_contact_with_only_invalid_dimensions_is_an_enquiry() -> None:
    wf, _sink = workflow(ExtractionResult(dimensions_in=INVALID_DIMS))
    first = wf.handle(REQ, email("0 x 0 x 0"))
    assert first.is_non_enquiry is False
    assert first.clarification is not None
    assert FieldName.DIMENSIONS_IN in first.clarification.asked_for


def test_first_contact_all_invalid_fields_produce_one_clarification() -> None:
    wf, sink = workflow(_all_invalid())
    first = wf.handle(REQ, email("pieces -5, 0 kg, 0 x 0 x 0"))
    assert first.is_non_enquiry is False
    assert first.clarification is not None
    assert set(first.clarification.asked_for) >= {
        FieldName.PCS,
        FieldName.WEIGHT_KG,
        FieldName.DIMENSIONS_IN,
    }
    assert sink.sent == [], "one draft, held for approval"


def test_an_unrelated_email_with_numbers_but_no_shipment_is_ignored() -> None:
    # BR-7: the model marks nothing STATED/INVALID for a receipt — its numbers
    # are not shipment fields — so recognition drops it.
    wf, _sink = workflow(ExtractionResult())  # nothing extracted
    outcome = wf.handle(REQ, email("Payment of Rs.499 received, ref 8823001"))
    assert outcome.is_non_enquiry is True


def test_a_quotation_email_with_no_shipment_information_is_ignored() -> None:
    wf, _sink = workflow(ExtractionResult())
    outcome = wf.handle(REQ, email("Re: quotation for the wedding — no details"))
    assert outcome.is_non_enquiry is True


def test_a_valid_quotation_is_still_accepted() -> None:
    wf, _sink = workflow(complete())
    outcome = wf.handle(REQ, email("full valid enquiry"))
    assert outcome.is_non_enquiry is False
    assert outcome.state is RequestState.VALIDATED


def test_a_valid_quotation_with_an_unusual_subject_is_still_accepted() -> None:
    wf, _sink = workflow(complete(pcs=ExtractedValue[int].not_stated()))
    outcome = wf.handle(
        REQ, email("Fwd: pls sort this asap!!!").model_copy(update={"subject": "urgent!!!"})
    )
    assert outcome.is_non_enquiry is False
    assert outcome.clarification is not None  # asks for the one missing field


# 8 & 9. Same-thread correlation of the corrected reply, and account isolation.
def test_same_thread_correlation_and_account_isolation_for_invalid_data() -> None:
    # Account A: enquiry with an invalid piece count → clarification; the reply on
    # the same thread correlates to the same request and merges the correction.
    router_a, _ex_a, _sink_a, store_a = _router(
        complete(pcs=INVALID_PCS), ExtractionResult(pcs=ExtractedValue[int].stated(15))
    )
    first = router_a.route(enquiry())
    router_a.approve(first.request_id, by="A. Operator")  # type: ignore[arg-type]

    second = router_a.route(reply())
    assert second.is_reply is True and second.ignored is False
    assert second.request_id == first.request_id, "correlated to the same request"
    assert second.outcome is not None and second.outcome.record.pcs == 15

    # Account B is a separate router/store — Account A's traffic never touched it.
    _router_b, _ex_b, _sink_b, store_b = _router()
    assert store_b.all_requests() == () and store_b.all_threads() == ()
    a_ids = {m for t in store_a.all_threads() for m in t.message_ids}
    assert a_ids and all("R-" not in i for i in a_ids)  # A's own thread anchors only
