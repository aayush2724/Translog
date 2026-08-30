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

The lane table is not the thing under test and is not being widened: refusing
an unknown place is the safety property (AMB-9). What is under test is that the
refusal stays the property of one request.

Everything is stubbed except the parts that matter — the router, the workflow,
the validator, the real rate stage and the real state machine all run.
"""

from __future__ import annotations

import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.test_gmail_thread import ScriptedExtractor, StubSource
from tests.unit.test_web_live import call

from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.config import Settings
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.routing import DEMO_LANES
from translog_quote.domain.shipment import CargoDimensions, DeliveryType
from translog_quote.domain.workflow import RequestState
from translog_quote.interface.web.live_session import LiveSequenceError, LiveSession
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


#: A place the demo lane table deliberately does not carry. Asserted rather
#: than assumed, so this suite fails loudly if the table ever grows to include
#: it instead of quietly testing nothing.
UNROUTABLE = "Hyderabad"


def test_the_unroutable_place_really_is_absent() -> None:
    assert UNROUTABLE.lower() not in DEMO_LANES


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
    )


# --- the regression -------------------------------------------------------------


def test_one_unroutable_request_does_not_stop_a_second_from_reaching_rate_selection(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The whole point. Before the fix this raised `UnknownPlace`."""
    session = _session(settings, sink, emails=(BAD, GOOD))

    session.poll()

    bad = session.requests[_only(session, UNROUTABLE)]
    good = session.requests[_only(session, "Ahmedabad")]

    assert bad.rates is None
    assert bad.rate_failure is not None
    assert good.rates is not None
    assert good.rates.selection is not None
    assert good.packet is not None
    assert good.state is RequestState.RATE_SELECTED


def test_the_poll_does_not_raise(settings: Settings, sink: CollectingEmailSink) -> None:
    session = _session(settings, sink, emails=(BAD,))

    session.poll()  # would have raised UnknownPlace

    assert session.requests


def test_the_bad_request_reports_why_it_has_no_rates(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    session = _session(settings, sink, emails=(BAD,))

    session.poll()

    failure = session.requests[_only(session, UNROUTABLE)].rate_failure
    assert failure is not None
    assert UNROUTABLE in failure


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


# --- what must NOT happen because of the failure --------------------------------


def test_the_failure_sends_no_email(settings: Settings, sink: CollectingEmailSink) -> None:
    session = _session(settings, sink, emails=(BAD,))

    session.poll()

    assert sink.sent == []


def test_the_failed_request_is_not_approvable(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """No packet, so no gate — the request cannot be approved by anyone."""
    session = _session(settings, sink, emails=(BAD,))
    session.poll()
    request_id = _only(session, UNROUTABLE)

    request = session.requests[request_id]
    assert request.packet is None
    assert request.awaiting_quotation_decision is False

    with pytest.raises(LiveSequenceError):
        session.decide(request_id, choice="approve", by="A. Operator")

    assert sink.sent == []


def test_the_failure_does_not_move_the_request(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """State-machine semantics are untouched: it stays where it was."""
    session = _session(settings, sink, emails=(BAD,))

    session.poll()

    assert session.requests[_only(session, UNROUTABLE)].state is RequestState.VALIDATED


# --- retry ----------------------------------------------------------------------


def test_the_request_is_retried_on_the_next_poll(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """No queue to drain and nothing to reset.

    The request is still VALIDATED with no rates, which is precisely what the
    search loop selects on — so once the cause is gone the next poll prices it.
    Simulated here by correcting the record the way a resolved lane would.
    """
    session = _session(settings, sink, emails=(BAD,))
    session.poll()
    request = session.requests[_only(session, UNROUTABLE)]
    assert request.rate_failure is not None

    request.record = request.record.model_copy(update={"origin": "Ahmedabad"})
    session.poll()

    assert request.rate_failure is None
    assert request.rates is not None
    assert request.packet is not None


# --- the HTTP surface -----------------------------------------------------------


def test_the_poll_endpoint_answers_200_not_500(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """What the browser actually saw: a 500 whose body the dashboard dropped."""
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
    failures = [r for r in requests if r.get("rate_failure")]
    assert len(failures) == 1
    assert UNROUTABLE in str(failures[0]["rate_failure"])
    assert any(r.get("awaiting_decision") for r in requests)


def _only(session: LiveSession, origin: str) -> str:
    """The one request whose extracted origin is this. Fails if ambiguous."""
    matches = [r.request_id for r in session.requests.values() if r.record.origin == origin]
    assert len(matches) == 1, f"expected exactly one request from {origin}, got {matches}"
    return matches[0]
