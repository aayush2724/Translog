"""The inbound router: correlate, process, merge, record the thread.

The model is scripted; **everything else is the real code path** — the real
policy, the real clarification workflow, the real merge, the real validator,
the real store. So these tests exercise the Phase 10.5 integration itself.

The case that matters most is the data-integrity section: a reply that repeats
none of the shipment must still leave the record complete.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from translog_quote.adapters.clock import FixedClock
from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.store import InMemoryStore
from translog_quote.domain.conversation import HeaderChainCorrelation, Thread
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.shipment import CargoDimensions, DeliveryType
from translog_quote.errors import IllegalTransition
from translog_quote.pipeline import ClarificationWorkflow, InboundRouter, RoutedMessage

ENQUIRY_ID = "<enquiry-1@mail.example.com>"
REPLY_ID = "<reply-1@mail.example.com>"
OTHER_ID = "<other@mail.example.com>"

#: The enquiry from the phase's test case: five fields, four gaps.
ENQUIRY_EXTRACTION = ExtractionResult(
    origin=ExtractedValue[str].stated("Ahmedabad"),
    destination=ExtractedValue[str].stated("Bahrain"),
    weight_kg=ExtractedValue[float].stated(500.0),
    dimensions_in=ExtractedValue[CargoDimensions].stated(
        CargoDimensions(length=34, width=24, height=6)
    ),
    cargo_type=ExtractedValue[str].stated("Non-Haz"),
)

#: The reply answers exactly the four questions and repeats nothing else.
REPLY_EXTRACTION = ExtractionResult(
    commodity=ExtractedValue[str].stated("Engineering components"),
    is_chemical=ExtractedValue[bool].stated(value=False),
    pcs=ExtractedValue[int].stated(10),
    delivery_type=ExtractedValue[DeliveryType].stated(DeliveryType.AIRPORT),
)


class ScriptedExtractor:
    def __init__(self, *results: ExtractionResult) -> None:
        self._results = list(results)
        self.calls: list[str] = []

    def extract_shipment(self, text: str) -> ExtractionResult:
        self.calls.append(text)
        return self._results.pop(0)

    def read_client_intent(self, text: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def enquiry(message_id: str = ENQUIRY_ID) -> RawEmail:
    return RawEmail(
        message_id=message_id,
        from_address="client@example.com",
        subject="Rate required - Ahmedabad to Bahrain",
        body_text="500 KG, Ahmedabad to Bahrain, 24 x 34 x 6 inches, Non-Haz.",
        received_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
    )


def reply(
    *,
    message_id: str = REPLY_ID,
    in_reply_to: str | None = ENQUIRY_ID,
    references: tuple[str, ...] = (ENQUIRY_ID,),
) -> RawEmail:
    return RawEmail(
        message_id=message_id,
        from_address="client@example.com",
        subject="Re: Rate required - Ahmedabad to Bahrain",
        body_text=(
            "Commodity: Engineering components\n"
            "Chemical: No\n"
            "Pieces: 10 cartons\n"
            "Delivery: Airport to airport"
        ),
        received_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC) + timedelta(hours=4),
        in_reply_to=in_reply_to,
        references=references,
    )


def build(
    *results: ExtractionResult, store: InMemoryStore | None = None
) -> tuple[InboundRouter, CollectingEmailSink, InMemoryStore]:
    """The real router over the real workflow, with only the model scripted."""
    shared = store or InMemoryStore()
    sink = CollectingEmailSink()
    workflow = ClarificationWorkflow(
        extractor=ScriptedExtractor(*results),
        sink=sink,
        store=shared,
        clock=FixedClock(),
    )
    router = InboundRouter(
        policy=HeaderChainCorrelation(),
        workflow=workflow,
        store=shared,
        new_request_id=lambda email: f"R-{email.message_id}",
    )
    return router, sink, shared


def ambiguous_reply() -> RawEmail:
    """A reply whose ancestry spans two known requests and whose In-Reply-To
    settles nothing — the chain must be the only signal, or the stronger
    header would resolve it and there would be no ambiguity to test."""
    return reply(in_reply_to=None, references=(ENQUIRY_ID, OTHER_ID))


def two_known_threads() -> InMemoryStore:
    store = InMemoryStore()
    store.save_thread(Thread(request_id="R-A", message_ids=(ENQUIRY_ID,)))
    store.save_thread(Thread(request_id="R-B", message_ids=(OTHER_ID,)))
    return store


def approve_and_reply(
    router: InboundRouter, message: RawEmail | None = None
) -> tuple[RoutedMessage, RoutedMessage]:
    """Run the phase's flow: enquiry, named human approval, reply."""
    first = router.route(enquiry())
    assert first.request_id is not None
    router.approve(first.request_id, by="ops.manager@translog.example")
    return first, router.route(message if message is not None else reply())


# --- routing a first enquiry ----------------------------------------------------


def test_a_first_enquiry_starts_a_new_request_and_is_recorded_as_a_thread() -> None:
    router, _, store = build(ENQUIRY_EXTRACTION)

    routed = router.route(enquiry())

    assert routed.is_reply is False
    assert routed.request_id == f"R-{ENQUIRY_ID}"
    assert store.all_threads() == (Thread(request_id=f"R-{ENQUIRY_ID}", message_ids=(ENQUIRY_ID,)),)


def test_the_incomplete_enquiry_drafts_a_clarification_and_sends_nothing() -> None:
    router, sink, _ = build(ENQUIRY_EXTRACTION)

    routed = router.route(enquiry())

    assert routed.outcome is not None
    assert routed.outcome.awaiting_approval
    assert sink.sent == []


# --- the reply path -------------------------------------------------------------


def test_a_real_reply_correlates_to_the_enquiry_it_answers() -> None:
    router, _, _ = build(ENQUIRY_EXTRACTION, REPLY_EXTRACTION)

    first, second = approve_and_reply(router)

    assert second.is_reply is True
    assert second.request_id == first.request_id


def test_the_reply_is_appended_to_the_same_thread() -> None:
    router, _, store = build(ENQUIRY_EXTRACTION, REPLY_EXTRACTION)

    approve_and_reply(router)

    assert store.all_threads() == (
        Thread(request_id=f"R-{ENQUIRY_ID}", message_ids=(ENQUIRY_ID, REPLY_ID)),
    )


def test_processing_the_same_message_twice_adds_no_second_anchor() -> None:
    """A re-run must not make one message two correlation anchors."""
    router, _, store = build(ENQUIRY_EXTRACTION, ENQUIRY_EXTRACTION)
    first = router.route(enquiry())
    assert first.request_id is not None
    router.approve(first.request_id, by="ops.manager@translog.example")

    router.route(enquiry())

    assert store.all_threads()[0].message_ids == (ENQUIRY_ID,)


# --- the critical data-integrity case -------------------------------------------


def test_the_merge_preserves_everything_the_reply_did_not_repeat() -> None:
    """The reply states four fields and repeats none of the shipment. All nine
    must be present afterwards — this is the whole point of Phase 10.5."""
    router, _, _ = build(ENQUIRY_EXTRACTION, REPLY_EXTRACTION)

    _, second = approve_and_reply(router)
    assert second.outcome is not None
    record = second.outcome.record

    # Carried over from the enquiry, absent from the reply.
    assert record.origin == "Ahmedabad"
    assert record.destination == "Bahrain"
    assert record.weight_kg == 500.0
    assert record.dimensions_in == CargoDimensions(length=34, width=24, height=6)
    assert record.cargo_type == "Non-Haz"
    # Supplied by the reply.
    assert record.commodity == "Engineering components"
    assert record.is_chemical is False
    assert record.pcs == 10
    assert record.delivery_type is DeliveryType.AIRPORT


def test_the_merged_shipment_validates() -> None:
    router, _, _ = build(ENQUIRY_EXTRACTION, REPLY_EXTRACTION)

    _, second = approve_and_reply(router)

    assert second.outcome is not None
    assert second.outcome.validation.is_valid
    assert second.outcome.is_complete


def test_the_reply_only_reports_the_fields_it_actually_filled() -> None:
    router, _, _ = build(ENQUIRY_EXTRACTION, REPLY_EXTRACTION)

    _, second = approve_and_reply(router)

    assert second.outcome is not None
    filled = {f.value for f in second.outcome.merge.changed}
    assert filled == {"commodity", "is_chemical", "pcs", "delivery_type"}


def test_the_client_is_never_asked_to_repeat_what_the_enquiry_already_said() -> None:
    """The draft asks only for the gaps, so a reply that answers them is
    sufficient — the client is never told to resend their shipment."""
    router, _, _ = build(ENQUIRY_EXTRACTION, REPLY_EXTRACTION)

    first, _ = approve_and_reply(router)
    assert first.outcome is not None
    assert first.outcome.clarification is not None
    asked = {f.value for f in first.outcome.clarification.asked_for}

    assert asked == {"commodity", "is_chemical", "pcs", "delivery_type"}
    for already_known in ("origin", "destination", "weight_kg", "dimensions_in"):
        assert already_known not in asked


def test_nothing_is_sent_anywhere_along_the_reply_path() -> None:
    router, sink, _ = build(ENQUIRY_EXTRACTION, REPLY_EXTRACTION)

    first = router.route(enquiry())
    assert sink.sent == []  # drafting sends nothing

    assert first.request_id is not None
    router.approve(first.request_id, by="ops.manager@translog.example")
    after_approval = len(sink.sent)
    router.route(reply())

    # The one sink hand-off in the whole run is the named human approval.
    assert after_approval == 1
    assert len(sink.sent) == 1


# --- refusals -------------------------------------------------------------------


def test_an_ambiguous_reply_is_refused_and_never_reaches_the_model() -> None:
    # No extraction is scripted: reaching the model would raise IndexError.
    router, sink, _ = build(store=two_known_threads())

    routed = router.route(ambiguous_reply())

    assert routed.was_refused
    assert routed.needs_manual_review
    assert routed.request_id is None
    assert sink.sent == []


def test_a_refused_message_is_not_recorded_against_any_thread() -> None:
    """Recording it would make the same guess one layer down: the next reply
    would then correlate to whichever thread it was filed under."""
    store = two_known_threads()
    router, _, _ = build(store=store)

    router.route(ambiguous_reply())

    for thread in store.all_threads():
        assert REPLY_ID not in thread.message_ids


def test_a_refusal_explains_itself() -> None:
    router, _, _ = build(store=two_known_threads())

    routed = router.route(ambiguous_reply())

    assert "more than one" in routed.reason
    assert "person" in routed.reason


def test_a_reply_to_an_unknown_parent_becomes_its_own_request_not_a_merge() -> None:
    router, _, store = build(ENQUIRY_EXTRACTION, ENQUIRY_EXTRACTION)
    router.route(enquiry())

    orphan = reply(in_reply_to="<never-seen@elsewhere.example>", references=())
    routed = router.route(orphan)

    assert routed.is_reply is False
    assert routed.request_id == f"R-{orphan.message_id}"
    # The original enquiry's record was not touched.
    original = store.get_request(f"R-{ENQUIRY_ID}")
    assert original is not None
    assert original.record.commodity is None


def test_a_reply_belonging_to_another_enquiry_merges_there_and_not_here() -> None:
    router, _, store = build(ENQUIRY_EXTRACTION, ENQUIRY_EXTRACTION, REPLY_EXTRACTION)
    first = router.route(enquiry())
    second = router.route(enquiry(message_id="<enquiry-2@mail.example.com>"))
    assert second.request_id is not None
    router.approve(second.request_id, by="ops.manager@translog.example")

    routed = router.route(reply(in_reply_to="<enquiry-2@mail.example.com>", references=()))

    assert routed.request_id == second.request_id
    assert routed.request_id != first.request_id
    untouched = store.get_request(str(first.request_id))
    assert untouched is not None
    assert untouched.record.commodity is None


# --- the approval gate is not bypassed ------------------------------------------


def test_a_reply_cannot_be_processed_while_a_draft_is_still_pending() -> None:
    """The transition table allows NEEDS_INFO -> CLARIFICATION_SENT only, so an
    unapproved draft structurally blocks the next round. The gate is not
    advisory, and the router does not route around it."""
    router, _, _ = build(ENQUIRY_EXTRACTION, REPLY_EXTRACTION)
    router.route(enquiry())

    with pytest.raises(IllegalTransition):
        router.route(reply())


def test_approval_requires_a_named_person() -> None:
    router, _, _ = build(ENQUIRY_EXTRACTION)
    routed = router.route(enquiry())
    assert routed.request_id is not None

    approval = router.approve(routed.request_id, by="ops.manager@translog.example")

    assert approval.by == "ops.manager@translog.example"


def test_the_pending_draft_is_visible_through_the_router() -> None:
    router, _, _ = build(ENQUIRY_EXTRACTION)
    routed = router.route(enquiry())
    assert routed.request_id is not None

    assert router.pending_draft(routed.request_id) is not None
