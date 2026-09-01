"""The demonstration that runs itself: a startup cutoff and a background poll.

Two changes are under test here, and they only make sense together.

**The cutoff.** A mailbox used for testing accumulates history, and after a
deploy every one of those old messages is as unread as a new one. So the moment
the process comes up is the boundary: mail that arrived before it is out of
scope and is never read, and the request the room watches is the one somebody
sends afterwards. Nothing is deleted from Gmail to achieve that.

**The poll.** There is no "check mail" control any more. The server reads the
mailbox on its own timer, through the same `LiveSession.poll()` the button used
to call, so an enquiry is extracted, validated, questioned and priced without
anybody clicking anything — and the two human gates are exactly where they
were, because `poll()` cannot pass either of them.

What these tests deliberately do *not* stub is the workflow. The router,
correlation, merge, validator, clarification loop, rate search and both gates
are real; the mailbox, the model and the outbound sink are not.
"""

from __future__ import annotations

import json
import tempfile
import threading
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.unit.test_gmail_thread import (
    ENQUIRY,
    ENQUIRY_EXTRACTION,
    REPLY,
    REPLY_EXTRACTION,
    ScriptedExtractor,
    StubSource,
)
from tests.unit.test_web_live import APPROVER, GrowingSource, call

from translog_quote import bootstrap
from translog_quote.adapters.clock import FixedClock
from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.store import JsonFileStore
from translog_quote.config import Settings
from translog_quote.domain.quotation import INTERNAL_SUBJECT_PREFIX
from translog_quote.domain.workflow import RequestState
from translog_quote.errors import TransientFailure
from translog_quote.interface.web import live_serialize
from translog_quote.interface.web.live_poller import LivePoller
from translog_quote.interface.web.live_session import LiveSession, build_live_session
from translog_quote.interface.web.server import DemoServer

if TYPE_CHECKING:
    from collections.abc import Iterator

    from translog_quote.domain.email import RawEmail

FAKE_KEY = "test-not-a-real-credential"
APPROVER_MAILBOX = "approvals@translog.example"

#: The demonstration's own clock. The fixture emails carry fixed dates, so the
#: cutoff is only meaningful against a fixed "now" — on the wall clock these
#: tests would mean one thing before the fixture's timestamp and another after.
NOW = ENQUIRY.received_at


@pytest.fixture
def settings() -> Settings:
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


def at(email: RawEmail, minutes: int) -> RawEmail:
    """The same message, arriving that many minutes from the fixture's own time."""
    return email.model_copy(update={"received_at": NOW + timedelta(minutes=minutes)})


def session_at_startup(
    settings: Settings,
    sink: CollectingEmailSink,
    *,
    source: object,
    extractions: tuple[object, ...],
) -> LiveSession:
    """A session as the server builds one: the cutoff is set before it polls.

    The clock is pinned, so "before startup" and "after startup" are decidable
    facts about the fixtures rather than a race with the wall clock.
    """
    session = LiveSession(
        settings,
        source=source,  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(*extractions),  # type: ignore[arg-type]
        clock=FixedClock(NOW),
    )
    session.start_demonstration()
    return session


def poller_for(session: LiveSession) -> LivePoller:
    """The background poller, driven a tick at a time instead of by a thread."""
    return LivePoller(session, lock=threading.Lock(), interval_seconds=60)


# --- A. mail that was already there is not this demonstration's -----------------


def test_mail_that_arrived_before_startup_is_never_read(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """A. Not read, rather than read and hidden. Extracting a mailbox of
    history to then not display it would cost one live model call per message
    and would put old shipments through the pipeline for no reason."""
    session = session_at_startup(
        settings,
        sink,
        source=StubSource(at(ENQUIRY, -90)),
        extractions=(),  # a single extraction call would raise
    )

    poller_for(session).poll_once()

    assert session.outside_demonstration == 1
    assert session.requests == {}
    assert live_serialize.snapshot(session)["requests"] == []


def test_the_cutoff_is_established_by_starting_the_server(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A, at the composition root. Nobody presses anything: building the live
    session is what fixes the cutoff, so the mailbox's history is out of scope
    before the first poll runs."""
    # The two collaborators the composition root would otherwise build for
    # real. Everything else — the demonstration file, the store, the router —
    # is the production wiring, which is the point of testing this function.
    monkeypatch.setattr(bootstrap, "build_gmail_email_sink", lambda _s: CollectingEmailSink())
    monkeypatch.setattr(bootstrap, "build_extractor", lambda _s: ScriptedExtractor())

    session = build_live_session(settings)

    assert session.demonstration.is_active is True
    assert session.demonstration.started_at is not None


def test_a_restart_does_not_resurrect_earlier_work_as_active(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """E, across a restart — the failure that made a deploy embarrassing.

    A persisted request used to be rebuilt into the live view, and the rate
    search runs over that view on every poll: old work did not merely appear on
    the dashboard, it advanced. It stays in the store, and stays out of the
    session.
    """
    first = session_at_startup(
        settings, sink, source=StubSource(ENQUIRY), extractions=(ENQUIRY_EXTRACTION,)
    )
    first.poll()
    first.approve_clarification(by=APPROVER)
    old_id = next(iter(first.requests))
    assert JsonFileStore(settings.demo.state_dir).get_request(old_id) is not None

    restarted = session_at_startup(settings, sink, source=StubSource(), extractions=())

    assert restarted.requests == {}, "the earlier request is not active work"
    assert JsonFileStore(settings.demo.state_dir).get_request(old_id) is not None, (
        "and nothing was deleted to achieve that"
    )


# --- B. a new enquiry is processed with nothing to click ------------------------


def test_a_new_enquiry_is_processed_by_the_background_poll(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """B. Extraction, validation and the clarification draft all happen on a
    poll nobody asked for. Nothing is sent: the draft is held at the gate."""
    session = session_at_startup(
        settings,
        sink,
        source=GrowingSource((), (at(ENQUIRY, 2),)),
        extractions=(ENQUIRY_EXTRACTION,),
    )
    poller = poller_for(session)

    poller.poll_once()  # an empty mailbox
    assert session.requests == {}

    poller.poll_once()  # the enquiry has arrived

    request = next(iter(session.requests.values()))
    assert request.state is RequestState.NEEDS_INFO
    assert request.clarification is not None
    assert request.awaiting_clarification_approval is True
    assert sink.sent == [], "a poll sends nothing; only a named person can"


def test_the_new_enquiry_is_the_one_the_dashboard_leads_with(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """E. History in the mailbox, one request on the screen."""
    session = session_at_startup(
        settings,
        sink,
        source=StubSource(at(ENQUIRY, -300), at(ENQUIRY, 5)),
        extractions=(ENQUIRY_EXTRACTION,),
    )

    poller_for(session).poll_once()

    snap = live_serialize.snapshot(session)
    assert len(snap["requests"]) == 1  # type: ignore[arg-type]
    assert snap["demonstration"]["following"] == 1  # type: ignore[index]
    assert snap["demonstration"]["outside_messages"] == 1  # type: ignore[index]


# --- C/D. replies: the current one is merged, an old one is not -----------------


def test_the_reply_to_the_current_enquiry_is_processed_automatically(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """C. The whole conversation on background polls: enquiry, clarification
    released by a person, reply merged, shipment validated and priced."""
    session = session_at_startup(
        settings,
        sink,
        source=GrowingSource((at(ENQUIRY, 1),), (at(ENQUIRY, 1), at(REPLY, 30))),
        extractions=(ENQUIRY_EXTRACTION, REPLY_EXTRACTION),
    )
    poller = poller_for(session)

    poller.poll_once()
    session.approve_clarification(by=APPROVER)  # the one human step in the middle
    poller.poll_once()

    request = next(iter(session.requests.values()))
    assert request.reply_received is True
    assert request.merged_fields, "the reply's own fields were merged in"
    assert request.rates is not None, "rate search ran off the back of the poll"
    assert request.packet is not None, "and the packet is waiting at the human gate"
    assert request.decision is None, "which nothing here may answer"


def test_a_reply_that_predates_the_startup_cutoff_is_ignored(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """D. Structural rather than a rule anyone has to remember: an old reply is
    never read, so there is no path by which it merges into anything."""
    session = session_at_startup(
        settings,
        sink,
        source=StubSource(at(REPLY, -300), at(ENQUIRY, 5)),
        extractions=(ENQUIRY_EXTRACTION,),
    )

    poller_for(session).poll_once()

    assert session.outside_demonstration == 1
    assert next(iter(session.requests.values())).reply_received is False


def test_an_old_reply_to_an_old_request_never_reopens_it(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """D, with the old request genuinely in the store. The reply correlates —
    its headers really do point at that thread — and it is still never read,
    because the cutoff is applied before correlation is."""
    first = session_at_startup(
        settings, sink, source=StubSource(ENQUIRY), extractions=(ENQUIRY_EXTRACTION,)
    )
    first.poll()
    first.approve_clarification(by=APPROVER)
    old_id = next(iter(first.requests))

    restarted = session_at_startup(
        settings,
        sink,
        source=StubSource(at(REPLY, -10)),  # arrived before this session came up
        extractions=(),  # extracting it would raise
    )
    poller_for(restarted).poll_once()

    assert restarted.outside_demonstration == 1
    assert old_id not in restarted.requests
    assert live_serialize.snapshot(restarted)["requests"] == []


# --- F. polling repeatedly costs nothing and changes nothing --------------------


def test_repeated_background_polls_do_not_process_anything_twice(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """F. The poller runs forever, so "the same message twice" is not a corner
    case — it is what every single tick after the first one would be."""
    extractor = ScriptedExtractor(ENQUIRY_EXTRACTION)
    session = LiveSession(
        settings,
        source=StubSource(at(ENQUIRY, 1)),  # type: ignore[arg-type]
        sink=sink,
        extractor=extractor,
        clock=FixedClock(NOW),
    )
    session.start_demonstration()
    poller = poller_for(session)

    poller.poll_once()
    audit_after_first = len(session.audit.events)
    for _ in range(5):
        poller.poll_once()

    assert len(extractor.calls) == 1, "the model was called once, for one message"
    assert len(session.requests) == 1
    assert len(session.audit.events) == audit_after_first, "no repeated evidence"
    assert sink.sent == [], "and nothing was sent by any of them"


def test_repeated_polls_after_the_quotation_send_nothing_further(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """F, past the last gate. A settled request must stay settled however many
    times the mailbox is read afterwards."""
    session = session_at_startup(
        settings,
        sink,
        source=GrowingSource((at(ENQUIRY, 1),), (at(ENQUIRY, 1), at(REPLY, 30))),
        extractions=(ENQUIRY_EXTRACTION, REPLY_EXTRACTION),
    )
    poller = poller_for(session)
    poller.poll_once()
    session.approve_clarification(by=APPROVER)
    poller.poll_once()
    request_id = next(iter(session.requests))
    session.decide(request_id, choice="approve", by=APPROVER)
    sent_after_decision = list(sink.sent)

    for _ in range(3):
        poller.poll_once()

    assert session.requests[request_id].state is RequestState.QUOTATION_SENT
    assert sink.sent == sent_after_decision, "no second quotation reached the client"


# --- G. the existing protections are not weakened by polling automatically ------


def test_our_own_approval_mail_is_still_never_ingested(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """G. The review packet lands in the same mailbox Translog reads. It is our
    words, not a client's, and a background poll must skip it exactly as the
    manual one did."""
    internal = ENQUIRY.model_copy(
        update={
            "message_id": "<internal@translog.example>",
            "subject": f"{INTERNAL_SUBJECT_PREFIX} review packet",
            "received_at": NOW + timedelta(minutes=3),
        }
    )
    session = session_at_startup(
        settings,
        sink,
        source=StubSource(internal),
        extractions=(),  # extracting our own mail would raise
    )

    poller_for(session).poll_once()

    assert session.skipped_internal == 1
    assert session.requests == {}


def test_the_clarification_gate_still_holds_under_automatic_polling(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """G. Polling forever must not become a way past a human gate. The reply is
    in the mailbox and stays unmerged for as many polls as it takes, because
    the question it answers has not been sent."""
    session = session_at_startup(
        settings,
        sink,
        source=StubSource(at(ENQUIRY, 1), at(REPLY, 30)),
        extractions=(ENQUIRY_EXTRACTION,),
    )
    poller = poller_for(session)

    for _ in range(4):
        poller.poll_once()

    request = next(iter(session.requests.values()))
    assert request.state is RequestState.NEEDS_INFO
    assert request.reply_received is False
    assert request.waiting_replies, "the operator is told a reply is waiting on them"
    assert sink.sent == []


# --- the poller itself ----------------------------------------------------------


class BrokenSession:
    """Stands in for a session whose mailbox read fails."""

    def __init__(self) -> None:
        self.calls = 0
        self.last_poll_error: str | None = None

    def poll(self) -> None:
        self.calls += 1
        raise TransientFailure("gmail is unreachable")


def test_a_failing_poll_does_not_end_the_polling() -> None:
    """A thread that died on its first bad poll would leave a dashboard that
    silently never updated again — a worse failure than the one it died of."""
    broken = BrokenSession()
    poller = LivePoller(broken, lock=threading.Lock(), interval_seconds=60)  # type: ignore[arg-type]

    assert poller.poll_once() is False
    assert poller.poll_once() is False
    assert broken.calls == 2
    assert broken.last_poll_error == "TransientFailure", "the class, never the detail"


def test_a_recovered_poll_clears_the_reported_failure(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    session = session_at_startup(settings, sink, source=StubSource(), extractions=())
    session.last_poll_error = "TransientFailure"

    assert poller_for(session).poll_once() is True
    assert session.last_poll_error is None
    assert session.last_poll_at is not None


def test_the_thread_starts_once_and_stops_cleanly(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    session = session_at_startup(settings, sink, source=StubSource(), extractions=())
    poller = LivePoller(session, lock=threading.Lock(), interval_seconds=60)

    poller.start()
    poller.start()  # idempotent: a double start must not make a second thread
    assert poller.is_running is True

    poller.stop()
    assert poller.is_running is False


# --- L. the server updates its own state, with no request from the browser ------


@pytest.fixture
def polling_server(settings: Settings, sink: CollectingEmailSink) -> Iterator[DemoServer]:
    """A live server polling on its own, as the deployed one does."""
    session = session_at_startup(
        settings,
        sink,
        source=GrowingSource((), (at(ENQUIRY, 2),)),
        extractions=(ENQUIRY_EXTRACTION,),
    )
    instance = DemoServer(
        ("127.0.0.1", 0), settings, live_session=session, poll_interval_seconds=0.1
    )
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()


def test_the_dashboard_state_changes_without_any_action_being_posted(
    polling_server: DemoServer, sink: CollectingEmailSink
) -> None:
    """L. The browser only ever reads. The enquiry appears because the server
    read the mailbox by itself, which is the entire requirement."""
    session = polling_server.live
    assert session is not None
    waited, deadline = 0.0, 5.0
    while not session.requests and waited < deadline:
        threading.Event().wait(0.05)
        waited += 0.05

    status, snap = call(polling_server, "GET", "/api/live/state")

    assert status == 200
    assert len(snap["requests"]) == 1, json.dumps(snap["requests"])  # type: ignore[arg-type]
    assert snap["poll"]["last_checked_at"] is not None  # type: ignore[index]
    assert sink.sent == [], "and it sent nothing while doing it"


def test_a_server_given_no_interval_polls_nothing(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The poller is opt-in, so a test that drives `poll()` itself gets a
    server that is not reading the mailbox behind its back."""
    session = session_at_startup(
        settings, sink, source=StubSource(at(ENQUIRY, 1)), extractions=(ENQUIRY_EXTRACTION,)
    )
    instance = DemoServer(("127.0.0.1", 0), settings, live_session=session)
    try:
        assert instance.poller is None
        assert session.requests == {}
    finally:
        instance.server_close()
