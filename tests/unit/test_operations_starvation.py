"""Operations mode: an old unresolved clarification draft must not starve newer mail.

The bug this guards against: the date-bounded operations fetch drains a window
oldest-first, up to a per-poll budget. A NEEDS_INFO draft commits nothing on
purpose (so a restart re-derives it), which pins the watermark at its message —
and, before the fix, that same draft sat at the front of the window every poll,
spending the whole budget re-reading itself so nothing newer was ever fetched.

The fix has two coordinated halves, exercised here end to end over the *real*
``GmailEmailSource`` (only the Gmail transport is a scripted stub):

- the fetch skips a message already handled this run, so the budget reaches the
  newer mail behind the drafts;
- ``_advance_watermark`` still holds the cutoff behind those unresolved drafts —
  now from the live requests rather than from re-reading them — so the safety
  invariant (never advance past uncommitted work) is preserved.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tests.unit.test_gmail_email_source import MAILBOX, PagingTransport, dated, malformed
from tests.unit.test_operations_restart import RECORD, _base_settings, _operations

from translog_quote.adapters.clock import FixedClock
from translog_quote.adapters.email import CollectingEmailSink, GmailEmailSource
from translog_quote.adapters.store import InMemoryStore
from translog_quote.domain.conversation import Thread
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.validation import validate_shipment
from translog_quote.domain.workflow import RequestState
from translog_quote.interface.web.live_session import LiveRequest, LiveSession

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
SEED = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)  # a cutoff before every message below

#: Only origin — enough to be an enquiry, far short of a quotable shipment, so
#: every message drafts a clarification and lands in NEEDS_INFO (uncommitted).
INCOMPLETE = ExtractionResult(origin=ExtractedValue[str].stated("Ahmedabad"))


class AlwaysExtractor:
    """Returns the same incomplete extraction for every message, and records the
    text of each call so a test can prove a settled draft is not re-extracted."""

    def __init__(self, result: ExtractionResult) -> None:
        self._result = result
        self.calls: list[str] = []

    def extract_shipment(self, text: str) -> ExtractionResult:
        self.calls.append(text)
        return self._result

    def read_client_intent(self, text: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _backlog_transport() -> PagingTransport:
    """Three old drafts and one newer first-contact message, listed newest-first
    the way Gmail returns them (reversed to oldest-first, the drafts lead)."""
    bodies = {
        "messages/d1": dated("d1", "Mon, 24 Aug 2026 09:00:00 +0000", "<d1@x>", subject="Draft 1"),
        "messages/d2": dated("d2", "Tue, 25 Aug 2026 09:00:00 +0000", "<d2@x>", subject="Draft 2"),
        "messages/d3": dated("d3", "Wed, 26 Aug 2026 09:00:00 +0000", "<d3@x>", subject="Draft 3"),
        "messages/new": dated(
            "new", "Thu, 27 Aug 2026 09:00:00 +0000", "<new@x>", subject="Newer enquiry"
        ),
    }
    return PagingTransport([(["new", "d3", "d2", "d1"], None)], bodies)


def _ops_session(
    settings: object, *, transport: PagingTransport, durable: InMemoryStore
) -> tuple[LiveSession, AlwaysExtractor]:
    """A real operations-mode session reading through a real ``GmailEmailSource``
    over a scripted transport, its ``seen`` wired to the session — exactly the
    wiring bootstrap performs, but injectable for the test."""
    extractor = AlwaysExtractor(INCOMPLETE)
    source = GmailEmailSource(
        transport,
        mailbox_address=MAILBOX,
        max_results=3,  # the per-poll budget: smaller than the 3-draft backlog + 1
        overlap_seconds=0.0,
    )
    session = LiveSession(
        settings,  # type: ignore[arg-type]
        source=source,
        sink=CollectingEmailSink(),
        extractor=extractor,
        durable=durable,
        clock=FixedClock(NOW),
    )
    source._seen = session._seen_message  # type: ignore[attr-defined]
    # Seed the cutoff only on a genuine first boot, exactly as operations startup
    # does: a restart over the same state dir must inherit the persisted (held)
    # watermark, not reset it — resetting would re-open the whole window.
    if session._demonstration.current.last_poll_watermark is None:
        session._demonstration.record_watermark(SEED)
    return session, extractor


def _mids(session: LiveSession) -> set[str]:
    return {r.enquiry.message_id for r in session.requests.values() if r.enquiry is not None}


def _rid(session: LiveSession, message_id: str) -> str:
    for rid, request in session.requests.items():
        if request.enquiry is not None and request.enquiry.message_id == message_id:
            return rid
    raise AssertionError(f"no request for {message_id}")


def _email(message_id: str, at: datetime) -> RawEmail:
    return RawEmail(
        message_id=message_id,
        from_address="client@example.com",
        subject="Rate please",
        body_text="body",
        received_at=at,
    )


# --- the end-to-end bug: a backlog of drafts no longer starves newer mail -------


def test_a_backlog_of_drafts_stops_starving_newer_mail_across_polls(tmp_path: object) -> None:
    settings = _operations(_base_settings(tmp_path), since=SEED)
    session, extractor = _ops_session(
        settings, transport=_backlog_transport(), durable=InMemoryStore()
    )

    # Poll 1: the budget is spent on the three oldest (the drafts). The newer
    # message is beyond it and is NOT fetched — the starvation, reproduced.
    session.poll()
    assert _mids(session) == {"<d1@x>", "<d2@x>", "<d3@x>"}
    assert all(r.state is RequestState.NEEDS_INFO for r in session.requests.values())
    # The watermark holds at the oldest uncommitted draft, never past it.
    d1_received = session.requests[_rid(session, "<d1@x>")].enquiry.received_at
    assert session.demonstration.last_poll_watermark == d1_received

    # Poll 2: the three drafts are now handled and skipped, so the budget reaches
    # the newer message at last — the fix.
    session.poll()
    assert _mids(session) == {"<d1@x>", "<d2@x>", "<d3@x>", "<new@x>"}
    assert session.requests[_rid(session, "<new@x>")].state is RequestState.NEEDS_INFO
    # Still pinned behind the oldest unresolved draft, even though it was skipped.
    assert session.demonstration.last_poll_watermark == d1_received

    # Each message was extracted exactly once: settled drafts are not re-processed
    # on the second poll, so no duplicate request or clarification is produced.
    assert len(extractor.calls) == 4
    assert len(session.requests) == 4

    # Poll 3: everything is seen; nothing new is fetched, nothing changes.
    session.poll()
    assert len(session.requests) == 4
    assert len(extractor.calls) == 4


# --- restart reconstruction preserves the behavior ------------------------------


def test_a_restart_re_derives_the_drafts_and_still_reaches_the_newer_mail(
    tmp_path: object,
) -> None:
    """The drafts commit nothing, so a restart finds an empty store and the held
    watermark. The first post-restart poll re-derives the drafts (re-reading
    them, as designed) and the next reaches the newer message — the same shape as
    before the restart, not a regression into starvation."""
    settings = _operations(_base_settings(tmp_path), since=SEED)

    first, _ = _ops_session(settings, transport=_backlog_transport(), durable=InMemoryStore())
    first.poll()
    first.poll()
    assert _mids(first) == {"<d1@x>", "<d2@x>", "<d3@x>", "<new@x>"}
    held = first.demonstration.last_poll_watermark

    # Restart: a fresh durable store (nothing was committed) and a fresh run
    # state, but the same persisted watermark under the same state dir.
    second, _ = _ops_session(settings, transport=_backlog_transport(), durable=InMemoryStore())
    assert second.requests == {}, "an uncommitted draft is not in the store to restore"
    assert second.demonstration.last_poll_watermark == held

    second.poll()  # re-reads and re-derives the three drafts
    assert _mids(second) == {"<d1@x>", "<d2@x>", "<d3@x>"}
    second.poll()  # and now reaches the newer message
    assert _mids(second) == {"<d1@x>", "<d2@x>", "<d3@x>", "<new@x>"}


# --- the watermark half, in isolation -------------------------------------------


def _bare_session(settings: object, *, durable: InMemoryStore) -> LiveSession:
    return LiveSession(
        settings,  # type: ignore[arg-type]
        source=None,  # type: ignore[arg-type]
        sink=CollectingEmailSink(),
        extractor=AlwaysExtractor(INCOMPLETE),
        durable=durable,
        clock=FixedClock(NOW),
    )


def test_advance_watermark_holds_behind_a_needs_info_draft_not_in_scope(tmp_path: object) -> None:
    """Part B, isolated. A NEEDS_INFO draft that the fetch has skipped is no
    longer in ``in_scope``, so the cutoff would jump past it — unless the live
    request pins it. It does: the watermark stays at the draft's enquiry even
    though only a newer, committed message is in scope this poll."""
    settings = _operations(_base_settings(tmp_path), since=SEED)
    session = _bare_session(settings, durable=InMemoryStore())
    session._demonstration.record_watermark(SEED)

    draft_enquiry = _email("<draft@x>", NOW.replace(hour=9))  # older, uncommitted
    draft_record = RECORD.model_copy(update={"request_id": "R-draft"})
    session.requests["R-draft"] = LiveRequest(
        request_id="R-draft",
        client_address="client@example.com",
        state=RequestState.NEEDS_INFO,
        record=draft_record,
        validation=validate_shipment(draft_record),
        enquiry=draft_enquiry,
    )
    # A newer message that IS committed (recorded in a durable thread).
    session._durable.save_thread(Thread(request_id="R-c", message_ids=("<committed@x>",)))
    committed = _email("<committed@x>", NOW.replace(hour=11))

    session._advance_watermark([committed])

    assert session.demonstration.last_poll_watermark == draft_enquiry.received_at


def test_advance_watermark_advances_once_no_draft_pins_it(tmp_path: object) -> None:
    """The counterpart: with the only request resolved (not NEEDS_INFO) and its
    message committed, nothing pins the cutoff and it advances to the newest."""
    settings = _operations(_base_settings(tmp_path), since=SEED)
    session = _bare_session(settings, durable=InMemoryStore())
    session._demonstration.record_watermark(SEED)

    session._durable.save_thread(Thread(request_id="R-v", message_ids=("<m@x>",)))
    v_record = RECORD.model_copy(update={"request_id": "R-v"})
    session.requests["R-v"] = LiveRequest(
        request_id="R-v",
        client_address="client@example.com",
        state=RequestState.VALIDATED,  # resolved: does not pin
        record=v_record,
        validation=validate_shipment(v_record),
        enquiry=_email("<m@x>", NOW.replace(hour=9)),
    )
    newest = _email("<m@x>", NOW.replace(hour=11))

    session._advance_watermark([newest])

    assert session.demonstration.last_poll_watermark == newest.received_at


# --- the seen callback, and multi-account isolation -----------------------------


def test_seen_message_reports_routed_and_committed_only(tmp_path: object) -> None:
    """The callback the session hands the fetch: True for a message handled this
    run or durably committed, False otherwise — the whole basis of the skip."""
    settings = _operations(_base_settings(tmp_path), since=SEED)
    session = _bare_session(settings, durable=InMemoryStore())

    assert session._seen_message("<unheard@x>") is False
    session._routed.add("<routed@x>")
    assert session._seen_message("<routed@x>") is True


def test_one_sessions_seen_set_does_not_leak_into_another(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Account isolation. ``seen`` is per-session state (the run's routed set and
    that account's own store), so what one mailbox's session has handled never
    causes another account's session to skip its own mail."""
    settings_a = _operations(_base_settings(tmp_path / "a"), since=SEED)
    settings_b = _operations(_base_settings(tmp_path / "b"), since=SEED)
    session_a = _bare_session(settings_a, durable=InMemoryStore())
    session_b = _bare_session(settings_b, durable=InMemoryStore())

    session_a._routed.add("<a-only@x>")

    assert session_a._seen_message("<a-only@x>") is True
    assert session_b._seen_message("<a-only@x>") is False


# --- a malformed message must not freeze the watermark (the Account A incident) --


def test_a_malformed_message_does_not_freeze_the_watermark(tmp_path: object) -> None:
    """End to end, over the real ``GmailEmailSource`` and ``LiveSession``. A
    malformed historical message sits at the front of the window; before the fix
    ``parse_gmail_message`` aborted the whole poll and the watermark never moved.
    Now it is quarantined, the newer valid message is processed, and the cutoff
    advances *past* the malformed message rather than freezing on it."""
    bodies = {
        "messages/bad": malformed("bad", "Mon, 24 Aug 2026 09:00:00 +0000", "<bad@x>"),
        "messages/good": dated(
            "good", "Tue, 25 Aug 2026 09:00:00 +0000", "<good@x>", subject="Real enquiry"
        ),
    }
    transport = PagingTransport([(["good", "bad"], None)], bodies)
    settings = _operations(_base_settings(tmp_path), since=SEED)
    session, _ = _ops_session(settings, transport=transport, durable=InMemoryStore())

    session.poll()  # must not raise despite the malformed message

    # The valid message became a request; the malformed one created nothing and
    # was quarantined in the source instead of aborting the poll.
    assert _mids(session) == {"<good@x>"}
    assert session._source._unparseable == {"bad"}  # type: ignore[attr-defined]
    # The watermark advanced to the valid message — strictly past the malformed
    # message (24 Aug) that used to freeze it.
    good_received = session.requests[_rid(session, "<good@x>")].enquiry.received_at
    assert good_received == datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
    assert session.demonstration.last_poll_watermark == good_received
    assert session.demonstration.last_poll_watermark > datetime(2026, 8, 24, 9, 0, tzinfo=UTC)


def test_a_minus_zero_date_header_does_not_freeze_the_mailbox(tmp_path: object) -> None:
    """End to end, over the real ``GmailEmailSource`` and ``LiveSession``. A
    ``Date: ... -0000`` header used to parse to a naive datetime; sorting it
    beside an ordinary aware one raised ``TypeError`` before any message was
    routed, on every poll, so the account never moved again."""
    bodies = {
        "messages/zoneless": dated(
            "zoneless", "Mon, 24 Aug 2026 09:00:00 -0000", "<zoneless@x>", subject="Enquiry A"
        ),
        "messages/normal": dated(
            "normal", "Tue, 25 Aug 2026 09:00:00 +0000", "<normal@x>", subject="Enquiry B"
        ),
    }
    transport = PagingTransport([(["normal", "zoneless"], None)], bodies)
    settings = _operations(_base_settings(tmp_path), since=SEED)
    session, extractor = _ops_session(settings, transport=transport, durable=InMemoryStore())

    session.poll()  # used to raise TypeError: can't compare offset-naive and offset-aware
    session.poll()  # and again on every later poll

    assert _mids(session) == {"<zoneless@x>", "<normal@x>"}
    assert len(extractor.calls) == 2, "each message extracted once"
    zoneless = session.requests[_rid(session, "<zoneless@x>")].enquiry
    assert zoneless is not None
    assert zoneless.received_at == datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    # Both drafts are unresolved, so the cutoff holds at the older one — and is
    # an aware instant a later comparison can use.
    watermark = session.demonstration.last_poll_watermark
    assert watermark == datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    assert watermark is not None and watermark.tzinfo is not None
