"""One unpriceable request must not take the poll down with it.

The failure this suite exists for was found in a live trial, not in a unit
test. A real enquiry named a place outside `DEMO_LANES`; `resolve_iata` refused
to guess — correctly — and the resulting `UnknownPlace` travelled out of the
rate-search loop, out of `poll`, and out of the request handler as a 500. The
consequences compounded:

- every *other* validated request in the same poll went unsearched, because the
  loop died on the first bad one;
- the rest of the mailbox went unread;
- the request persisted at VALIDATED, so it was reloaded and re-attempted on
  every later poll — the demonstration was wedged until somebody edited a
  table.

Since the lane table was removed, the demo resolver accepts any place a client
names — so the failure is now reproduced the way production would hit it: a
resolver that cannot identify one particular location. Refusing rather than
guessing remains the safety property (AMB-9); what is under test is that the
refusal stays the property of one request.

Everything is stubbed except the parts that matter — the router, the workflow,
the validator, the real rate stage and the real state machine all run.
"""

from __future__ import annotations

import tempfile
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.test_gmail_thread import ScriptedExtractor, StubSource
from tests.unit.test_web_live import call

from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.config import Settings
from translog_quote.domain.clarification import UnresolvedReason
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.rates import LocationRef
from translog_quote.domain.shipment import CargoDimensions, DeliveryType, FieldName
from translog_quote.domain.workflow import RequestState
from translog_quote.errors import UnresolvedLocation
from translog_quote.interface.web.live_session import LiveRequest, LiveSequenceError, LiveSession
from translog_quote.interface.web.server import DemoServer

FAKE_KEY = "test-not-a-real-credential"
APPROVER_MAILBOX = "approvals@translog.example"


@pytest.fixture
def settings() -> Settings:
    """Declared here rather than imported, so the fixture is this file's own."""
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    return base.model_copy(
        update={
            "openrouter": base.openrouter.model_copy(update={"api_key": FAKE_KEY}),
            "demo": base.demo.model_copy(update={"state_dir": Path(tempfile.mkdtemp())}),
            "gmail": base.gmail.model_copy(
                update={
                    "test_address": "translog@example.com",
                    "sender_address": "translog@example.com",
                    "approver_address": APPROVER_MAILBOX,
                    "send_enabled": True,
                }
            ),
        }
    )


@pytest.fixture
def sink() -> CollectingEmailSink:
    return CollectingEmailSink()


#: The place the stub provider below cannot identify. Any string would do: the
#: point is a provider-side refusal, not a property of this particular name.
UNROUTABLE = "Hyderabad"


class PartialResolver:
    """A provider that can identify some places and not others.

    Exactly the production shape: a real lookup answers for most locations and
    refuses for some. It resolves by *returning what it was given* and never by
    deriving a code, so nothing here can accidentally model a guess.
    """

    resolver_id = "test-partial"

    def resolve(self, place: str) -> LocationRef:
        if UNROUTABLE.lower() in place.lower():
            raise UnresolvedLocation(f"{place!r} could not be identified by the provider")
        return LocationRef(stated=place)


def _email(message_id: str, subject: str, minutes: int) -> RawEmail:
    return RawEmail(
        message_id=message_id,
        from_address="client@example.com",
        subject=subject,
        body_text="See details.",
        received_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC) + timedelta(minutes=minutes),
    )


def _complete(origin: str, destination: str) -> ExtractionResult:
    """Every field the validator requires, so the request validates at once.

    A complete enquiry is the shape that reaches rate search on the first pass,
    which is the shape this failure occurs in.
    """
    return ExtractionResult(
        origin=ExtractedValue[str].stated(origin),
        destination=ExtractedValue[str].stated(destination),
        weight_kg=ExtractedValue[float].stated(500.0),
        dimensions_in=ExtractedValue[CargoDimensions].stated(
            CargoDimensions(length=34, width=24, height=6)
        ),
        commodity=ExtractedValue[str].stated("Engineering components"),
        cargo_type=ExtractedValue[str].stated("Non-Haz"),
        is_chemical=ExtractedValue[bool].stated(value=False),
        pcs=ExtractedValue[int].stated(10),
        delivery_type=ExtractedValue[DeliveryType].stated(DeliveryType.AIRPORT),
        ship_date=ExtractedValue[date].stated(date(2026, 9, 15)),
    )


BAD = _email("<bad-1@mail.example.com>", "Rate required - Hyderabad to Bahrain", 0)
GOOD = _email("<good-1@mail.example.com>", "Rate required - Ahmedabad to Bahrain", 5)

BAD_EXTRACTION = _complete(UNROUTABLE, "Bahrain")
GOOD_EXTRACTION = _complete("Ahmedabad", "Bahrain")


def _session(
    settings: Settings, sink: CollectingEmailSink, *, emails: tuple[RawEmail, ...]
) -> LiveSession:
    """Extractions are scripted in the order the session consumes them."""
    order = {BAD.message_id: BAD_EXTRACTION, GOOD.message_id: GOOD_EXTRACTION}
    return LiveSession(
        settings,
        source=StubSource(*emails),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(*(order[e.message_id] for e in emails)),  # type: ignore[arg-type]
        resolver=PartialResolver(),
    )


# --- the regression: an unresolvable place is isolated as a clarification -------


def _clarifying(session: LiveSession) -> LiveRequest:
    """The one request the session is holding for a client clarification.

    Identified by the held draft rather than by origin, because drafting clears
    the unresolved place (Change 4) so `_only(session, UNROUTABLE)` would no
    longer find it."""
    held = [r for r in session.requests.values() if r.clarification is not None]
    assert len(held) == 1, f"expected exactly one held clarification, got {len(held)}"
    return held[0]


def test_one_unresolvable_place_does_not_stop_a_second_from_reaching_selection(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The whole point. Before the isolation this raised out of the poll; now
    the unpriceable request is turned into a held client clarification, and the
    good request still reaches selection."""
    session = _session(settings, sink, emails=(BAD, GOOD))

    session.poll()

    bad = _clarifying(session)
    good = session.requests[_only(session, "Ahmedabad")]

    assert bad.rates is None
    assert bad.rate_failure is None  # not a dead end any more
    assert bad.state is RequestState.NEEDS_INFO
    assert good.rates is not None
    assert good.rates.selection is not None
    assert good.packet is not None
    assert good.state is RequestState.RATE_SELECTED


def test_the_poll_does_not_raise(settings: Settings, sink: CollectingEmailSink) -> None:
    session = _session(settings, sink, emails=(BAD,))

    session.poll()  # would have raised out of the whole poll

    assert session.requests


def test_the_held_request_asks_the_client_for_the_airport(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The unresolved place becomes a clarification that names it, in plain
    words, with no internal vocabulary."""
    session = _session(settings, sink, emails=(BAD,))

    session.poll()

    draft = _clarifying(session).clarification
    assert draft is not None
    (asked,) = draft.unresolved
    assert asked.field is FieldName.ORIGIN
    assert asked.reason is UnresolvedReason.AMBIGUOUS
    assert UNROUTABLE in asked.detail  # the client's own wording, preserved
    assert UNROUTABLE in draft.body_text
    lowered = draft.body_text.lower()
    for internal in ("unresolvedlocation", "resolver", "canonical", "traceback"):
        assert internal not in lowered


def test_the_order_of_the_two_does_not_matter(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The good request must be searched whether it is seen before or after.

    Worth pinning separately: the original defect was order-dependent — a
    request searched before the bad one survived, and everything after it did
    not, which is exactly the kind of bug that looks intermittent.
    """
    session = _session(settings, sink, emails=(GOOD, BAD))

    session.poll()

    assert session.requests[_only(session, "Ahmedabad")].packet is not None


# --- what must NOT happen because of the clarification --------------------------


def test_the_clarification_sends_no_email_before_approval(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    session = _session(settings, sink, emails=(BAD,))

    session.poll()

    assert sink.sent == []


def test_the_held_request_has_no_quotation_to_decide(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """It awaits a clarification approval, not a quotation decision: there is no
    packet, so the quotation gate refuses it."""
    session = _session(settings, sink, emails=(BAD,))
    session.poll()
    request = _clarifying(session)

    assert request.packet is None
    assert request.awaiting_quotation_decision is False
    assert request.awaiting_clarification_approval is True

    with pytest.raises(LiveSequenceError):
        session.decide(request.request_id, choice="approve", by="A. Operator")

    assert sink.sent == []


def test_the_unresolved_place_moves_the_request_to_needs_info(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The change this task introduced: instead of dead-ending at VALIDATED with
    a rate failure, the request moves to NEEDS_INFO to ask the client."""
    session = _session(settings, sink, emails=(BAD,))

    session.poll()

    assert _clarifying(session).state is RequestState.NEEDS_INFO


# --- idempotency ----------------------------------------------------------------


def test_polling_twice_does_not_draft_a_second_clarification(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """A held request is NEEDS_INFO, which the search loop no longer selects, so
    a second poll neither re-drafts nor sends."""
    session = _session(settings, sink, emails=(BAD,))
    session.poll()
    first = _clarifying(session).clarification

    session.poll()

    held = _clarifying(session)
    assert held.clarification is first  # the same draft, not a new one
    assert held.state is RequestState.NEEDS_INFO
    assert sink.sent == []


# --- the HTTP surface -----------------------------------------------------------


def test_the_poll_endpoint_answers_200_not_500(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """What the browser saw before isolation was a 500 whose body the dashboard
    dropped. Now the unresolved request is a held clarification, the good one
    awaits a decision, and the endpoint answers 200 either way."""
    server = DemoServer(
        ("127.0.0.1", 0), settings, live_session=_session(settings, sink, emails=(BAD, GOOD))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = call(server, "POST", "/api/live/poll", {})
    finally:
        server.shutdown()
        server.server_close()

    assert status == 200, payload
    requests = payload["requests"]
    assert isinstance(requests, list)
    assert [r for r in requests if r.get("rate_failure")] == []
    assert any(r.get("awaiting_clarification") for r in requests)
    assert any(r.get("awaiting_decision") for r in requests)


def _only(session: LiveSession, origin: str) -> str:
    """The one request whose extracted origin is this. Fails if ambiguous."""
    matches = [r.request_id for r in session.requests.values() if r.record.origin == origin]
    assert len(matches) == 1, f"expected exactly one request from {origin}, got {matches}"
    return matches[0]
