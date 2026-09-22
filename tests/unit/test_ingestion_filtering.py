"""Only Translog quotation requests and their replies enter the workflow.

Everything else — Paytm receipts, newsletters, notifications, an unrelated mail
that merely says "quotation" — is filtered at the ingestion/routing boundary
(``InboundRouter.route``): no request, no extraction beyond content recognition,
no clarification, no rate search, nothing on the dashboard. Recognition is by
sender (automated/bulk) and by content (the extraction found no shipment), never
by a subject keyword.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tests.unit.test_inbound_router import (
    ENQUIRY_EXTRACTION,
    REPLY_EXTRACTION,
    ScriptedExtractor,
    enquiry,
    reply,
)

from translog_quote.adapters.clock import FixedClock
from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.store import InMemoryStore
from translog_quote.domain.conversation import HeaderChainCorrelation
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractionResult
from translog_quote.pipeline import ClarificationWorkflow, InboundRouter

NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def _router(*results: ExtractionResult, store: InMemoryStore | None = None):  # type: ignore[no-untyped-def]
    """One account's routing boundary — real policy/workflow/store, scripted
    model — returning the extractor so a test can assert whether it was called."""
    shared = store or InMemoryStore()
    sink = CollectingEmailSink()
    extractor = ScriptedExtractor(*results)
    workflow = ClarificationWorkflow(
        extractor=extractor, sink=sink, store=shared, clock=FixedClock()
    )
    router = InboundRouter(
        policy=HeaderChainCorrelation(),
        workflow=workflow,
        store=shared,
        new_request_id=lambda email: f"R-{email.message_id}",
    )
    return router, extractor, sink, shared


def _mail(sender: str, *, subject: str = "hello", mid: str = "<x@noise>") -> RawEmail:
    return RawEmail(
        message_id=mid,
        from_address=sender,
        subject=subject,
        body_text="Your weekly update is here. Click to view. Unsubscribe anytime.",
        received_at=NOW,
    )


# 1. Valid quotation request → appears (becomes a request).
def test_valid_quotation_request_becomes_a_request() -> None:
    router, extractor, _sink, _store = _router(ENQUIRY_EXTRACTION)

    routed = router.route(enquiry())

    assert routed.ignored is False
    assert routed.outcome is not None and routed.request_id is not None
    assert extractor.calls, "a real enquiry is extracted and processed"


# 2. Valid request with an unusual subject → still appears (content, not subject).
def test_valid_request_with_an_unusual_subject_still_appears() -> None:
    router, _extractor, _sink, _store = _router(ENQUIRY_EXTRACTION)

    routed = router.route(enquiry().model_copy(update={"subject": "Fwd: pls sort this asap!!!"}))

    assert routed.ignored is False
    assert routed.outcome is not None


# 3. Clarification reply to an existing thread → continues the existing request.
def test_reply_to_an_existing_thread_continues_the_request() -> None:
    router, _extractor, _sink, _store = _router(ENQUIRY_EXTRACTION, REPLY_EXTRACTION)
    first = router.route(enquiry())
    router.approve(first.request_id, by="A. Operator")  # type: ignore[arg-type]

    routed = router.route(reply())

    assert routed.is_reply is True
    assert routed.ignored is False
    assert routed.request_id == first.request_id, "merged into the same request"
    assert routed.outcome is not None
    # the corrected data merged into the existing shipment
    assert routed.outcome.record.commodity == "Engineering components"
    assert routed.outcome.record.origin == "Ahmedabad"


# 4. Paytm / unrelated automated mail → ignored, with NO extraction.
def test_a_paytm_email_is_ignored_without_extraction() -> None:
    router, extractor, sink, store = _router()  # no scripted results — none is needed

    routed = router.route(_mail("no-reply@paytm.com", subject="Payment of Rs.499 successful"))

    assert routed.ignored is True
    assert routed.outcome is None, "it never reached the workflow"
    assert extractor.calls == [], "the model was never called"
    assert sink.sent == []
    # recorded as seen so it is never re-examined, but no request exists
    assert store.all_requests() == ()
    assert store.all_threads(), "recorded as seen"


# 5. Newsletter / notification → ignored (automated senders), no extraction.
def test_newsletters_and_notifications_are_ignored() -> None:
    for sender in ("newsletter@brand.example", "notifications@service.example"):
        router, extractor, _sink, _store = _router()
        routed = router.route(_mail(sender, subject="What's new this week"))
        assert routed.ignored is True, sender
        assert extractor.calls == [], sender


# 6. Unrelated email whose SUBJECT contains "quotation" → NOT a request.
def test_an_unrelated_email_mentioning_quotation_is_not_a_request() -> None:
    # A human sender (passes the automated pre-filter) whose message states no
    # shipment — recognised as unrelated by content, not by the subject word.
    router, extractor, sink, _store = _router(ExtractionResult())

    routed = router.route(
        _mail("friend@gmail.example", subject="Re: quotation for the wedding photos")
    )

    assert routed.ignored is True
    assert routed.outcome is not None and routed.outcome.is_non_enquiry is True
    assert extractor.calls, "content was inspected (not judged on the subject word)"
    assert sink.sent == [], "no clarification drafted or sent"


# 7. Account A filters independently of Account B.
def test_account_a_filters_independently_of_b() -> None:
    router_a, _ex_a, _sink_a, store_a = _router(ENQUIRY_EXTRACTION)
    router_b, ex_b, _sink_b, store_b = _router()

    a = router_a.route(enquiry(message_id="<a-enq@x>"))
    b = router_b.route(_mail("no-reply@paytm.com", mid="<b-paytm@x>"))

    assert a.ignored is False and a.outcome is not None  # A processed its enquiry
    assert b.ignored is True and ex_b.calls == []  # B ignored its noise, no extraction
    # neither account's mail leaked into the other's store
    a_ids = {m for t in store_a.all_threads() for m in t.message_ids}
    b_ids = {m for t in store_b.all_threads() for m in t.message_ids}
    assert a_ids == {"<a-enq@x>"} and b_ids == {"<b-paytm@x>"}


# 8. Account B filters independently of Account A (the mirror).
def test_account_b_filters_independently_of_a() -> None:
    router_a, ex_a, _sink_a, _store_a = _router()
    router_b, _ex_b, _sink_b, _store_b = _router(ENQUIRY_EXTRACTION)

    a = router_a.route(_mail("newsletter@brand.example", mid="<a-news@x>"))
    b = router_b.route(enquiry(message_id="<b-enq@x>"))

    assert a.ignored is True and ex_a.calls == []
    assert b.ignored is False and b.outcome is not None


# 9. Duplicate-message dedup remains intact — including for ignored mail.
def test_a_repeated_ignored_message_records_one_anchor_and_no_extraction() -> None:
    router, extractor, _sink, store = _router()
    paytm = _mail("no-reply@paytm.com", mid="<dup@x>")

    router.route(paytm)
    router.route(paytm)  # the same message again

    anchors = [m for t in store.all_threads() for m in t.message_ids]
    assert anchors.count("<dup@x>") == 1, "recorded exactly once"
    assert extractor.calls == [], "never extracted, however many times it arrives"


# 10. No extraction call for ignored messages (the guarantee, made explicit).
def test_no_extraction_occurs_for_ignored_messages() -> None:
    router, extractor, _sink, _store = _router()

    for sender in ("no-reply@paytm.com", "newsletter@brand.example", "do-not-reply@bank.example"):
        router.route(_mail(sender, mid=f"<{sender}>"))

    assert extractor.calls == [], "not one model call for any ignored message"
