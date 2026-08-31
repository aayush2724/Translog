"""A clarification is released for the request it was approved on, and no other.

Reproduced from the deployed demo. Several enquiries were awaiting
clarification at once; the operator opened a brand-new Coimbatore → Istanbul
request and clicked "Approve & send to client". Gmail then showed the
clarification threaded into an *older* Chennai → Frankfurt conversation, and a
second click threaded another into an older Singapore → Dubai conversation. The
new request never received a clarification at all.

Nothing was wrong with reply correlation. `HeaderChainCorrelation` matches on
RFC 5322 header chains only, request ids are a SHA-256 of the enquiry's own
Message-ID, and no thread state is shared between requests. What went wrong was
*which draft was released*:

    server._live_approve_clarification   dropped the browser's `request_id`
    LiveSession._one_awaiting_clarification   returned held[0]

— so the approval landed on whichever request happened to be first in
dictionary order, and the clarification was correctly threaded (In-Reply-To
that request's enquiry) into the wrong client's conversation.

These tests pin that the released draft belongs to the named request, that the
outbound message threads to *that* request's enquiry, and that an unnamed
approval refuses rather than guessing when more than one draft is held.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.test_gmail_thread import ScriptedExtractor, StubSource

from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.routing import StatedLocationResolver
from translog_quote.config import Settings
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.workflow import RequestState
from translog_quote.interface.web.live_session import LiveSequenceError, LiveSession

APPROVER = "A. Operator"

#: Two unrelated enquiries, each incomplete, each in its own Gmail thread —
#: exactly the deployed situation. Delivery type is deliberately absent so both
#: land in NEEDS_INFO and hold a draft simultaneously.
OLD = RawEmail(
    message_id="<old-chennai-frankfurt@mail.gmail.com>",
    from_address="client-a@example.com",
    subject="Air Freight Quote - Chennai to Frankfurt",
    body_text="Chennai to Frankfurt, 500 kg.",
    received_at=datetime(2026, 8, 30, 18, 52, tzinfo=UTC),
)
NEW = RawEmail(
    message_id="<new-coimbatore-istanbul@mail.gmail.com>",
    from_address="client-b@example.com",
    subject="Air Freight Quote Request: Coimbatore to Istanbul",
    body_text="Coimbatore to Istanbul, 500 kg.",
    received_at=datetime(2026, 8, 31, 11, 33, tzinfo=UTC),
)


def _incomplete(origin: str, destination: str) -> ExtractionResult:
    """Enough to be an enquiry, short of a delivery type — so it needs asking."""
    return ExtractionResult(
        origin=ExtractedValue[str].stated(origin),
        destination=ExtractedValue[str].stated(destination),
        weight_kg=ExtractedValue[float].stated(500.0),
    )


@pytest.fixture
def settings() -> Settings:
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    return base.model_copy(
        update={
            "openrouter": base.openrouter.model_copy(update={"api_key": "test-not-a-credential"}),
            "demo": base.demo.model_copy(update={"state_dir": Path(tempfile.mkdtemp())}),
            "gmail": base.gmail.model_copy(
                update={
                    "test_address": "translog@example.com",
                    "sender_address": "translog@example.com",
                    "approver_address": "approvals@translog.example",
                    "send_enabled": True,
                }
            ),
        }
    )


@pytest.fixture
def two_pending(settings: Settings) -> tuple[LiveSession, CollectingEmailSink]:
    """A session holding two clarification drafts at once, oldest first."""
    sink = CollectingEmailSink()
    session = LiveSession(
        settings,
        source=StubSource(OLD, NEW),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(  # type: ignore[arg-type]
            _incomplete("Chennai", "Frankfurt"), _incomplete("Coimbatore", "Istanbul")
        ),
        resolver=StatedLocationResolver(),
    )
    session.poll()
    held = [r for r in session.requests.values() if r.awaiting_clarification_approval]
    assert len(held) == 2, "both enquiries must be awaiting clarification"
    return session, sink


def _by_origin(session: LiveSession, origin: str) -> str:
    matching = [r.request_id for r in session.requests.values() if r.record.origin == origin]
    assert len(matching) == 1, f"expected one request from {origin}, got {matching}"
    return matching[0]


# --- the regression -------------------------------------------------------------


def test_approving_the_new_request_mails_the_new_request(
    two_pending: tuple[LiveSession, CollectingEmailSink],
) -> None:
    """The bug, exactly. Before the fix this mailed the Chennai client."""
    session, sink = two_pending
    new_id = _by_origin(session, "Coimbatore")

    session.approve_clarification(by=APPROVER, request_id=new_id)

    assert len(sink.sent) == 1
    sent = sink.sent[0]
    assert sent.to_address == NEW.from_address
    # Threading is what put it in the wrong Gmail conversation: the outbound
    # message must reply to the *named* request's enquiry, never another's.
    assert sent.in_reply_to == NEW.message_id
    assert sent.in_reply_to != OLD.message_id


def test_the_other_request_is_untouched(
    two_pending: tuple[LiveSession, CollectingEmailSink],
) -> None:
    """No cross-contamination: the older request keeps its unsent draft."""
    session, sink = two_pending
    new_id = _by_origin(session, "Coimbatore")
    old_id = _by_origin(session, "Chennai")

    session.approve_clarification(by=APPROVER, request_id=new_id)

    old = session.requests[old_id]
    assert old.state is RequestState.NEEDS_INFO
    assert old.awaiting_clarification_approval is True
    assert session._router.pending_draft(old_id) is not None
    assert [m.to_address for m in sink.sent] == [NEW.from_address]


def test_approving_the_old_request_mails_the_old_request(
    two_pending: tuple[LiveSession, CollectingEmailSink],
) -> None:
    """Targeting works in both directions, not just for the newest."""
    session, sink = two_pending
    old_id = _by_origin(session, "Chennai")

    session.approve_clarification(by=APPROVER, request_id=old_id)

    assert sink.sent[0].in_reply_to == OLD.message_id
    assert session.requests[_by_origin(session, "Coimbatore")].state is RequestState.NEEDS_INFO


def test_each_request_can_be_approved_independently(
    two_pending: tuple[LiveSession, CollectingEmailSink],
) -> None:
    """Two approvals, two clients, two threads — no shared correlation state."""
    session, sink = two_pending
    session.approve_clarification(by=APPROVER, request_id=_by_origin(session, "Coimbatore"))
    session.approve_clarification(by=APPROVER, request_id=_by_origin(session, "Chennai"))

    threaded = {m.to_address: m.in_reply_to for m in sink.sent}
    assert threaded == {NEW.from_address: NEW.message_id, OLD.from_address: OLD.message_id}


# --- refusing to guess ----------------------------------------------------------


def test_an_unnamed_approval_refuses_while_several_are_pending(
    two_pending: tuple[LiveSession, CollectingEmailSink],
) -> None:
    """The old fallback picked held[0]. It must now decline to choose."""
    session, sink = two_pending

    with pytest.raises(LiveSequenceError, match="must be named"):
        session.approve_clarification(by=APPROVER)

    assert sink.sent == [], "refusing must not mail anybody"


def test_an_unnamed_approval_still_works_for_a_lone_request(settings: Settings) -> None:
    """A single-request demonstration is unambiguous and keeps working."""
    sink = CollectingEmailSink()
    session = LiveSession(
        settings,
        source=StubSource(NEW),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(_incomplete("Coimbatore", "Istanbul")),  # type: ignore[arg-type]
        resolver=StatedLocationResolver(),
    )
    session.poll()

    session.approve_clarification(by=APPROVER)

    assert sink.sent[0].in_reply_to == NEW.message_id


def test_naming_a_request_with_no_draft_is_refused(
    two_pending: tuple[LiveSession, CollectingEmailSink],
) -> None:
    session, sink = two_pending
    new_id = _by_origin(session, "Coimbatore")
    session.approve_clarification(by=APPROVER, request_id=new_id)

    with pytest.raises(LiveSequenceError, match="no clarification awaiting approval"):
        session.approve_clarification(by=APPROVER, request_id=new_id)

    assert len(sink.sent) == 1, "the spent draft must not be released twice"


def test_an_unknown_request_id_is_refused(
    two_pending: tuple[LiveSession, CollectingEmailSink],
) -> None:
    session, sink = two_pending

    with pytest.raises(LiveSequenceError):
        session.approve_clarification(by=APPROVER, request_id="R-GMAIL-nonexistent")

    assert sink.sent == []


# --- the reply still correlates to the request it belongs to --------------------


def test_the_clients_reply_merges_into_its_own_request(
    two_pending: tuple[LiveSession, CollectingEmailSink],
    settings: Settings,
) -> None:
    """End to end: the reply to the new clarification must not touch the old
    request. Correlation is by header chain, so this proves the two threads
    stayed separate all the way through."""
    session, sink = two_pending
    new_id = _by_origin(session, "Coimbatore")
    old_id = _by_origin(session, "Chennai")
    session.approve_clarification(by=APPROVER, request_id=new_id)

    reply = RawEmail(
        message_id="<reply-coimbatore@mail.gmail.com>",
        from_address=NEW.from_address,
        subject=f"Re: {NEW.subject}",
        body_text="Delivery: Airport to airport",
        received_at=NEW.received_at + timedelta(hours=1),
        in_reply_to=NEW.message_id,
        references=(NEW.message_id,),
    )
    session._source = StubSource(OLD, NEW, reply)  # type: ignore[assignment,arg-type]
    session._router._workflow._extractor = ScriptedExtractor(  # type: ignore[attr-defined]
        _incomplete("Coimbatore", "Istanbul")
    )
    session.poll()

    assert session.requests[new_id].reply_received is True
    assert session.requests[old_id].reply_received is False
    assert session.requests[old_id].state is RequestState.NEEDS_INFO
