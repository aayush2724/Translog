"""A clarification that could not be sent is still waiting to be sent.

The reported symptom, from the deployed app: a client had already replied, the
operator clicked "Approve & send to client", and the request stayed at
"Clarification awaiting approval" / NEEDS_INFO — permanently, with the reply
never merging and the workflow never reaching rate search.

The mechanism was ordering. `approve_clarification` popped the pending draft on
the way in and only then handed the message to the sink, so any failure inside
the sink — an expired credential, a token without the send scope, a provider
outage — destroyed the draft on its way past:

- the state never advanced, so the request stayed in NEEDS_INFO;
- the client's reply stayed deferred behind a clarification that never went out
  (the transition table permits no way out of NEEDS_INFO except
  CLARIFICATION_SENT, and the router defers replies until it is taken);
- the draft was gone, so approving again answered "no clarification draft is
  awaiting approval" — the operator could not retry;
- and the audit trail carried a `clarification_approved` with no
  `clarification_sent` beside it, which reads as though a message went out.

These tests pin the corrected shape: the send happens first, and the draft is
consumed only once the provider has accepted it. Nothing here weakens the gate
— every send below still requires an explicit named approval — and nothing
fakes a delivery: the failing sink raises exactly as a real one does.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.test_gmail_thread import (
    ENQUIRY,
    ENQUIRY_EXTRACTION,
    REPLY,
    REPLY_EXTRACTION,
    ScriptedExtractor,
    StubSource,
)

from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.routing import StatedLocationResolver
from translog_quote.config import Settings
from translog_quote.domain.email import OutboundMessage
from translog_quote.domain.workflow import RequestState
from translog_quote.errors import PermanentFailure
from translog_quote.interface.web.live_session import LiveSession

APPROVER = "A. Operator"


class FailingThenWorkingSink:
    """A sink that refuses until told otherwise, exactly as Gmail would.

    Models the real failure rather than a sentinel: `GmailEmailSink.send`
    raises `PermanentFailure` out of the transport when the provider rejects
    the request, and appends to `sent` only after acceptance.
    """

    def __init__(self, *, failures: int) -> None:
        self.remaining_failures = failures
        self.sent: list[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> None:
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise PermanentFailure(
                "Gmail rejected the send (403): Request had insufficient authentication scopes."
            )
        self.sent.append(message)


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


def session_with(settings: Settings, sink: object, *, emails: tuple[object, ...]) -> LiveSession:
    return LiveSession(
        settings,
        source=StubSource(*emails),  # type: ignore[arg-type]
        sink=sink,  # type: ignore[arg-type]
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION, REPLY_EXTRACTION),  # type: ignore[arg-type]
        resolver=StatedLocationResolver(),
    )


def only(session: LiveSession) -> object:
    return next(iter(session.requests.values()))


# --- a failed send leaves everything exactly as it was --------------------------


def test_a_failed_send_keeps_the_draft_pending(settings: Settings) -> None:
    """The regression. Before the fix the draft was gone and could not be retried."""
    sink = FailingThenWorkingSink(failures=1)
    session = session_with(settings, sink, emails=(ENQUIRY,))
    session.poll()

    with pytest.raises(PermanentFailure):
        session.approve_clarification(by=APPROVER)

    request = only(session)
    assert request.state is RequestState.NEEDS_INFO  # type: ignore[attr-defined]
    assert request.awaiting_clarification_approval is True  # type: ignore[attr-defined]
    # The workflow's own held draft, not the interface's display copy: the two
    # are separate objects, and only this one decides whether a retry can work.
    assert session._router.pending_draft(request.request_id) is not None  # type: ignore[attr-defined]
    assert sink.sent == [], "nothing was delivered, so nothing may be recorded as sent"


def test_a_failed_send_can_be_retried_by_the_same_person(settings: Settings) -> None:
    """The operator fixes the credential and clicks again. It must work."""
    sink = FailingThenWorkingSink(failures=1)
    session = session_with(settings, sink, emails=(ENQUIRY,))
    session.poll()
    with pytest.raises(PermanentFailure):
        session.approve_clarification(by=APPROVER)

    session.approve_clarification(by=APPROVER)

    request = only(session)
    assert request.state is RequestState.CLARIFICATION_SENT  # type: ignore[attr-defined]
    assert len(sink.sent) == 1


def test_a_failed_send_records_no_clarification_sent_event(settings: Settings) -> None:
    """An approval with no send beside it must not read as a delivery."""
    sink = FailingThenWorkingSink(failures=1)
    session = session_with(settings, sink, emails=(ENQUIRY,))
    session.poll()

    with pytest.raises(PermanentFailure):
        session.approve_clarification(by=APPROVER)

    names = [event.event.value for event in session.audit.events]
    assert "clarification_sent" not in names
    assert "state_changed" in names  # the earlier extracted -> needs_info move


def test_a_failed_send_persists_no_state_change(settings: Settings) -> None:
    sink = FailingThenWorkingSink(failures=1)
    session = session_with(settings, sink, emails=(ENQUIRY,))
    session.poll()

    with pytest.raises(PermanentFailure):
        session.approve_clarification(by=APPROVER)

    stored = session._durable.get_request(only(session).request_id)  # type: ignore[attr-defined]
    assert stored is None or stored.state is not RequestState.CLARIFICATION_SENT


# --- the reported end-to-end scenario -------------------------------------------


def test_the_reply_merges_and_the_workflow_continues_after_a_retried_send(
    settings: Settings,
) -> None:
    """The whole reported flow, with the send failing once on the way through.

    The client's reply is already in the mailbox while the clarification is
    still held — which is exactly the situation described — so it must stay
    deferred until the clarification actually goes out, then merge.
    """
    sink = FailingThenWorkingSink(failures=1)
    session = session_with(settings, sink, emails=(ENQUIRY, REPLY))
    session.poll()

    request = only(session)
    assert request.state is RequestState.NEEDS_INFO  # type: ignore[attr-defined]
    assert session.blocked_messages == 1, "the reply waits behind the unsent clarification"

    # The failing send: nothing moves, and the draft survives.
    with pytest.raises(PermanentFailure):
        session.approve_clarification(by=APPROVER)
    assert only(session).state is RequestState.NEEDS_INFO  # type: ignore[attr-defined]

    # The retry succeeds, and the workflow resumes from where it stopped.
    session.approve_clarification(by=APPROVER)
    assert only(session).state is RequestState.CLARIFICATION_SENT  # type: ignore[attr-defined]

    session.poll()

    settled = only(session)
    assert settled.reply_received is True  # type: ignore[attr-defined]
    assert settled.state is RequestState.RATE_SELECTED  # type: ignore[attr-defined]
    assert settled.packet is not None  # type: ignore[attr-defined]
    assert settled.awaiting_quotation_decision is True  # type: ignore[attr-defined]
    # One clarification went out. The quotation has not: that gate is untouched.
    assert len(sink.sent) == 1


def test_the_quotation_gate_still_requires_its_own_decision(settings: Settings) -> None:
    """The fix must not have loosened anything downstream."""
    sink = FailingThenWorkingSink(failures=0)
    session = session_with(settings, sink, emails=(ENQUIRY, REPLY))
    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()

    request = only(session)
    assert request.decision is None  # type: ignore[attr-defined]
    assert len(sink.sent) == 1, "still only the clarification; no quotation without a decision"

    session.decide(request.request_id, choice="approve", by=APPROVER)  # type: ignore[attr-defined]

    assert len(sink.sent) == 3, "review packet + client quotation"


# --- the audit trail tells the truth either way ---------------------------------


def test_a_successful_send_still_records_both_events(settings: Settings) -> None:
    sink = CollectingEmailSink()
    session = session_with(settings, sink, emails=(ENQUIRY,))
    session.poll()

    before = datetime.now(UTC)
    session.approve_clarification(by=APPROVER)

    names = [event.event.value for event in session.audit.events]
    assert names.count("clarification_approved") == 1
    assert names.count("clarification_sent") == 1
    assert only(session).state is RequestState.CLARIFICATION_SENT  # type: ignore[attr-defined]
    assert before is not None
