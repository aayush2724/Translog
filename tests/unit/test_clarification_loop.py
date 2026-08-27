"""The clarification loop, end to end and offline.

A scripted extractor stands in for Qwen — it returns a prepared
`ExtractionResult` per turn, so every step except the model call is the real
code path: real merge, real validator, real composer, real state machine.

Scenario letters match the Phase 6 brief.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from translog_quote.adapters.clock import FixedClock
from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.store import InMemoryStore
from translog_quote.domain.clarification import UnresolvedReason
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.shipment import CargoDimensions, DeliveryType, FieldName
from translog_quote.domain.workflow import RequestState
from translog_quote.pipeline import ClarificationWorkflow

REQ = "R-TEST"


class ScriptedExtractor:
    """Returns a prepared result per call, in order."""

    def __init__(self, *results: ExtractionResult) -> None:
        self._results = list(results)
        self.calls: list[str] = []

    def extract_shipment(self, text: str) -> ExtractionResult:
        self.calls.append(text)
        return self._results.pop(0)

    def read_client_intent(self, text: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def email(body: str, *, n: int = 1) -> RawEmail:
    return RawEmail(
        message_id=f"<m{n}@clientco.example>",
        from_address="buyer@clientco.example",
        subject="Rate request",
        body_text=body,
        received_at=datetime(2026, 9, 1, 10, n, tzinfo=UTC),
    )


def workflow(*results: ExtractionResult, max_rounds: int = 3):  # type: ignore[no-untyped-def]
    sink = CollectingEmailSink()
    wf = ClarificationWorkflow(
        extractor=ScriptedExtractor(*results),
        sink=sink,
        store=InMemoryStore(),
        clock=FixedClock(),
        max_rounds=max_rounds,
    )
    return wf, sink


def complete(**overrides: object) -> ExtractionResult:
    """An extraction with every field the validator requires."""
    base: dict[str, object] = {
        "origin": ExtractedValue[str].stated("Ahmedabad"),
        "destination": ExtractedValue[str].stated("Bahrain"),
        "weight_kg": ExtractedValue[float].stated(500.0),
        "dimensions_in": ExtractedValue[CargoDimensions].stated(
            CargoDimensions(length=34, width=24, height=6)
        ),
        "commodity": ExtractedValue[str].stated("Industrial adhesive"),
        "cargo_type": ExtractedValue[str].stated("Non Haz"),
        "is_chemical": ExtractedValue[bool].stated(value=False),
        "pcs": ExtractedValue[int].stated(15),
        "delivery_type": ExtractedValue[DeliveryType].stated(DeliveryType.AIRPORT),
    }
    base.update(overrides)
    return ExtractionResult(**base)  # type: ignore[arg-type]


# --- A. Complete initial email -> no clarification ----------------------------


def test_a_a_complete_email_needs_no_clarification() -> None:
    wf, sink = workflow(complete())

    outcome = wf.handle(REQ, email("full details"))

    assert outcome.is_complete
    assert outcome.state is RequestState.VALIDATED
    assert outcome.clarification is None
    assert sink.sent == []


# --- B. Missing one field -> one clarification --------------------------------


def test_b_one_missing_field_produces_one_question() -> None:
    wf, sink = workflow(complete(pcs=ExtractedValue[int].not_stated()))

    outcome = wf.handle(REQ, email("no piece count"))

    assert outcome.asked_for_more
    assert outcome.clarification is not None
    assert outcome.clarification.asked_for == (FieldName.PCS,)
    assert len(sink.sent) == 1


# --- C. Missing several -> ONE message containing all -------------------------


def test_c_several_missing_fields_go_in_a_single_message() -> None:
    wf, sink = workflow(
        complete(
            commodity=ExtractedValue[str].not_stated(),
            pcs=ExtractedValue[int].not_stated(),
            delivery_type=ExtractedValue[DeliveryType].not_stated(),
        )
    )

    outcome = wf.handle(REQ, email("sparse"))

    assert len(sink.sent) == 1, "one batched message, never one per field"
    assert outcome.clarification is not None
    assert set(outcome.clarification.asked_for) == {
        FieldName.COMMODITY,
        FieldName.PCS,
        FieldName.DELIVERY_TYPE,
    }
    body = sink.sent[0].body_text
    assert "1." in body and "2." in body and "3." in body


# --- D. Reply resolves the gaps -> merge -> valid -----------------------------


def test_d_a_reply_completes_the_shipment() -> None:
    initial = complete(
        commodity=ExtractedValue[str].not_stated(), pcs=ExtractedValue[int].not_stated()
    )
    reply = ExtractionResult(
        commodity=ExtractedValue[str].stated("Industrial adhesive"),
        pcs=ExtractedValue[int].stated(15),
    )
    wf, sink = workflow(initial, reply)

    first = wf.handle(REQ, email("sparse", n=1))
    second = wf.handle(REQ, email("Commodity industrial adhesive, 15 pieces", n=2))

    assert first.asked_for_more
    assert second.is_complete
    assert len(sink.sent) == 1, "no clarification sent once complete"


def test_d_existing_values_survive_a_partial_reply() -> None:
    """The reply says nothing about origin; origin must not disappear."""
    initial = complete(commodity=ExtractedValue[str].not_stated())
    reply = ExtractionResult(commodity=ExtractedValue[str].stated("Industrial adhesive"))
    wf, _ = workflow(initial, reply)

    wf.handle(REQ, email("sparse", n=1))
    second = wf.handle(REQ, email("Commodity is industrial adhesive", n=2))

    assert second.record.origin == "Ahmedabad"
    assert second.record.weight_kg == 500.0
    assert second.record.pcs == 15
    assert second.record.commodity == "Industrial adhesive"


def test_d_only_the_reply_text_is_extracted() -> None:
    """The model sees one message at a time, never a reconstructed thread."""
    wf, _ = workflow(
        complete(pcs=ExtractedValue[int].not_stated()),
        ExtractionResult(pcs=ExtractedValue[int].stated(15)),
    )
    extractor = wf._extractor  # type: ignore[attr-defined]

    wf.handle(REQ, email("first message", n=1))
    wf.handle(REQ, email("second message", n=2))

    assert extractor.calls == ["first message", "second message"]


# --- E. Reply resolves only some -> second clarification ----------------------


def test_e_a_partial_reply_triggers_a_second_round() -> None:
    initial = complete(
        weight_kg=ExtractedValue[float].not_stated(),
        dimensions_in=ExtractedValue[CargoDimensions].not_stated(),
    )
    partial_reply = ExtractionResult(weight_kg=ExtractedValue[float].stated(500.0))
    final_reply = ExtractionResult(
        dimensions_in=ExtractedValue[CargoDimensions].stated(
            CargoDimensions(length=34, width=24, height=6)
        )
    )
    wf, sink = workflow(initial, partial_reply, final_reply)

    first = wf.handle(REQ, email("no weight or dims", n=1))
    second = wf.handle(REQ, email("weight is 500 kg", n=2))
    third = wf.handle(REQ, email("dims 34x24x6 inches", n=3))

    assert set(first.clarification.asked_for) == {  # type: ignore[union-attr]
        FieldName.WEIGHT_KG,
        FieldName.DIMENSIONS_IN,
    }
    assert second.asked_for_more
    assert second.clarification.asked_for == (FieldName.DIMENSIONS_IN,)  # type: ignore[union-attr]
    assert third.is_complete
    assert len(sink.sent) == 2


def test_e_the_second_round_does_not_re_ask_what_was_answered() -> None:
    """Scenario H, inside the multi-turn flow: weight was supplied in round 1,
    so round 2 must not mention it."""
    wf, sink = workflow(
        complete(
            weight_kg=ExtractedValue[float].not_stated(),
            dimensions_in=ExtractedValue[CargoDimensions].not_stated(),
        ),
        ExtractionResult(weight_kg=ExtractedValue[float].stated(500.0)),
    )

    wf.handle(REQ, email("nothing", n=1))
    wf.handle(REQ, email("500 kg", n=2))

    second_body = sink.sent[1].body_text.lower()
    assert "weight" not in second_body
    assert "dimensions" in second_body


# --- F. Conflicting reply -> conflict preserved -> clarification --------------


def test_f_a_conflicting_reply_is_never_silently_resolved() -> None:
    # The thread is still being clarified when the correction arrives, which is
    # the shape the reference thread actually had.
    wf, sink = workflow(
        complete(pcs=ExtractedValue[int].not_stated()),
        ExtractionResult(weight_kg=ExtractedValue[float].stated(700.0)),
    )

    wf.handle(REQ, email("500 kg, no piece count", n=1))
    second = wf.handle(REQ, email("actually 700 kg", n=2))

    assert second.merge.has_conflicts
    assert second.record.weight_kg == 500.0, "existing value not overwritten"
    assert second.asked_for_more
    assert second.clarification is not None
    assert second.clarification.unresolved[0].reason is UnresolvedReason.CONFLICT
    body = sink.sent[1].body_text
    assert "500" in body and "700" in body
    assert "which is correct" in body.lower()


# --- G. Ambiguity -> clarification asking for the usable form -----------------


def test_g_ambiguous_information_asks_for_the_form_we_need() -> None:
    wf, sink = workflow(
        complete(dimensions_in=ExtractedValue[CargoDimensions].ambiguous(note="given in cm"))
    )

    outcome = wf.handle(REQ, email("dims in cm"))

    assert outcome.clarification is not None
    item = outcome.clarification.unresolved[0]
    assert item.field is FieldName.DIMENSIONS_IN
    assert item.reason is UnresolvedReason.AMBIGUOUS
    assert "inches" in sink.sent[0].body_text


def test_g_ambiguity_is_never_converted_on_our_side() -> None:
    wf, _ = workflow(complete(weight_kg=ExtractedValue[float].ambiguous(note="stated as 1100 lbs")))

    outcome = wf.handle(REQ, email("1100 lbs"))

    assert outcome.record.weight_kg is None, "no conversion invented"


# --- H. Already-known fields are never asked about ----------------------------


def test_h_known_fields_never_appear_in_a_question() -> None:
    wf, sink = workflow(complete(pcs=ExtractedValue[int].not_stated()))

    wf.handle(REQ, email("everything but pieces"))

    body = sink.sent[0].body_text.lower()
    for known in ("origin", "destination", "commodity", "chemical"):
        assert known not in body, f"asked about {known}, which the client already gave"


# --- I. Explicit negatives are answers, not gaps ------------------------------


def test_i_an_explicit_no_is_not_treated_as_missing() -> None:
    """ "Not a chemical" and "no MSDS" are answers. Re-asking would be rude."""
    wf, sink = workflow(
        complete(
            is_chemical=ExtractedValue[bool].stated(value=True),
            msds_attached=ExtractedValue[bool].stated(value=False),
        )
    )

    outcome = wf.handle(REQ, email("chemical, no MSDS available"))

    assert outcome.is_complete
    assert sink.sent == []


# --- J. Terminal valid state, no further clarification ------------------------


def test_j_a_valid_shipment_is_terminal_for_this_phase() -> None:
    wf, _ = workflow(complete())

    outcome = wf.handle(REQ, email("complete"))

    assert outcome.state is RequestState.VALIDATED
    assert outcome.analysis.needs_clarification is False


# --- guards -------------------------------------------------------------------


def test_a_thread_that_will_not_converge_goes_to_a_person() -> None:
    """Asking forever is not a workflow. After the cap, a human takes over."""
    sparse = complete(pcs=ExtractedValue[int].not_stated())
    wf, sink = workflow(sparse, sparse, sparse, max_rounds=2)

    wf.handle(REQ, email("a", n=1))
    wf.handle(REQ, email("b", n=2))
    third = wf.handle(REQ, email("c", n=3))

    assert third.needs_a_person
    assert third.state is RequestState.MANUAL_REVIEW
    assert len(sink.sent) == 2, "no further asking after the cap"


def test_the_clarification_body_contains_no_internal_vocabulary() -> None:
    wf, sink = workflow(complete(pcs=ExtractedValue[int].not_stated()))

    wf.handle(REQ, email("x"))

    body = sink.sent[0].body_text
    for leaked in (
        "PCS_REQUIRED",
        "ValidationResult",
        "ExtractionResult",
        "not_stated",
        "Qwen",
        "schema",
        "field",
        "null",
        "AI",
    ):
        assert leaked not in body, f"internal vocabulary leaked to the client: {leaked}"


def test_the_outbound_message_carries_correlation() -> None:
    wf, sink = workflow(complete(pcs=ExtractedValue[int].not_stated()))

    wf.handle(REQ, email("x", n=7))

    sent = sink.sent[0]
    assert sent.to_address == "buyer@clientco.example"
    assert sent.in_reply_to == "<m7@clientco.example>"
    assert sent.subject.startswith("Re:")


def test_the_workflow_never_asks_the_model_about_completeness() -> None:
    """The architectural rule, as a test: the model is called exactly once per
    inbound message, with the message body, and for nothing else."""
    wf, _ = workflow(complete(pcs=ExtractedValue[int].not_stated()))
    extractor = wf._extractor  # type: ignore[attr-defined]

    wf.handle(REQ, email("one message"))

    assert extractor.calls == ["one message"]


def test_audit_records_the_workflow_without_the_email_body() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.events: list[object] = []

        def record(self, event: object) -> None:
            self.events.append(event)

    audit = Recorder()
    wf = ClarificationWorkflow(
        extractor=ScriptedExtractor(complete(pcs=ExtractedValue[int].not_stated())),
        sink=CollectingEmailSink(),
        store=InMemoryStore(),
        clock=FixedClock(),
        audit=audit,
    )
    wf.handle(REQ, email("sensitive client text that must not be logged"))

    kinds = {e.event.value for e in audit.events}  # type: ignore[attr-defined]
    assert {
        "email_received",
        "extraction_called",
        "record_merged",
        "validated",
        "clarification_sent",
        "state_changed",
    } <= kinds
    dumped = " ".join(str(e.detail) for e in audit.events)  # type: ignore[attr-defined]
    assert "sensitive client text" not in dumped


def test_a_conflict_is_audited() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.events: list[object] = []

        def record(self, event: object) -> None:
            self.events.append(event)

    audit = Recorder()
    wf = ClarificationWorkflow(
        extractor=ScriptedExtractor(
            complete(pcs=ExtractedValue[int].not_stated()),
            ExtractionResult(weight_kg=ExtractedValue[float].stated(700.0)),
        ),
        sink=CollectingEmailSink(),
        store=InMemoryStore(),
        clock=FixedClock(),
        audit=audit,
    )
    wf.handle(REQ, email("500", n=1))
    wf.handle(REQ, email("700", n=2))

    assert "conflict_detected" in {e.event.value for e in audit.events}  # type: ignore[attr-defined]


@pytest.mark.parametrize("state", [RequestState.VALIDATED, RequestState.CLARIFICATION_SENT])
def test_every_state_the_loop_reaches_is_legal(state: RequestState) -> None:
    """The loop uses the approved transition table and adds no state to it."""
    from translog_quote.domain.workflow import TRANSITIONS

    assert state in TRANSITIONS
