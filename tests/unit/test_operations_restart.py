"""Operations mode: a restart resumes rather than starting a fresh demonstration.

Demonstration mode is the rehearsal default — every boot draws a fresh cutoff at
``now`` and leads with the enquiry sent next. Operations mode is what the Render
deployment runs: a restart (every deploy is one) must not empty the dashboard,
so non-terminal requests are restored from the durable store, terminal ones come
back as hidden history, and the mail cutoff is the last successful poll — not
``now`` — so mail that arrived during a deploy is still read exactly once.

These exercise the seams that make that true: the store-based restore, the
watermark that never passes uncommitted work, the approval-card re-derivation,
and the serialized view the dashboard and the healthcheck read.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from tests.unit.test_gmail_thread import (
    ENQUIRY,
    ENQUIRY_EXTRACTION,
    ScriptedExtractor,
    StubSource,
)

from translog_quote import bootstrap
from translog_quote.adapters.clock import FixedClock
from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.store import InMemoryStore, JsonFileStore
from translog_quote.config import Settings
from translog_quote.domain.conversation import Thread
from translog_quote.domain.email import RawEmail
from translog_quote.domain.goods_type import record_fingerprint
from translog_quote.domain.shipment import CargoDimensions, RequestSource, ShipmentRecord
from translog_quote.domain.validation import validate_shipment
from translog_quote.domain.workflow import QuotationRequest, RequestState
from translog_quote.errors import PermanentFailure
from translog_quote.interface.web import live_serialize
from translog_quote.interface.web.demonstration import Demonstration, DemonstrationFile
from translog_quote.interface.web.live_session import LiveRequest, LiveSession, build_live_session
from translog_quote.pipeline.audit import AuditEventType

APPROVER = "ops@translog.example"
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

RECORD = ShipmentRecord(
    request_id="R-1",
    source=RequestSource.EMAIL,
    origin="Ahmedabad",
    destination="Bahrain",
    weight_kg=500.0,
    dimensions_in=CargoDimensions(length=34, width=24, height=6),
)

FULL_RECORD = RECORD.model_copy(
    update={
        "pcs": 10,
        "commodity": "Engineering components",
        "cargo_type": "general cargo",
        "is_chemical": False,
        "ship_date": date(2026, 9, 20),
    }
)


def _base_settings(state_dir: object) -> Settings:
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    return base.model_copy(
        update={
            "openrouter": base.openrouter.model_copy(update={"api_key": "k"}),
            "demo": base.demo.model_copy(update={"state_dir": state_dir}),
            "gmail": base.gmail.model_copy(
                update={
                    "test_address": "t@example.com",
                    "sender_address": "t@example.com",
                    "approver_address": APPROVER,
                    "send_enabled": True,
                }
            ),
        }
    )


def _operations(settings: Settings, *, since: datetime | None) -> Settings:
    return settings.model_copy(
        update={
            "demo": settings.demo.model_copy(
                update={"startup_mode": "operations", "operations_since": since}
            )
        }
    )


def _session(settings: Settings, *, durable: InMemoryStore) -> LiveSession:
    return LiveSession(
        settings,
        source=StubSource(),
        sink=CollectingEmailSink(),
        extractor=ScriptedExtractor(),
        durable=durable,
        clock=FixedClock(NOW),
    )


def _email(message_id: str, at: datetime) -> RawEmail:
    return RawEmail(
        message_id=message_id,
        from_address="client@example.com",
        subject="Rate please",
        body_text="body",
        received_at=at,
    )


def _stored(request_id: str, state: RequestState) -> QuotationRequest:
    return QuotationRequest(
        request_id=request_id,
        state=state,
        record=RECORD.model_copy(update={"request_id": request_id}),
        client_address="client@example.com",
    )


# --- restore from the store, not from demonstration.json ------------------------


def test_operations_restore_recovers_a_dropped_request(tmp_path: object) -> None:
    """Req 2. Today's deploys reset demonstration.json's request_ids to empty.
    Operations mode restores from the STORE, so a committed request comes back
    regardless — exactly what recovers the requests a deploy hid."""
    settings = _operations(_base_settings(tmp_path), since=NOW - timedelta(hours=1))
    # Simulate today's reset: a started demonstration that follows nothing.
    DemonstrationFile(tmp_path).save(Demonstration(started_at=NOW, request_ids=()))
    durable = InMemoryStore()
    durable.save_request(_stored("R-1", RequestState.VALIDATED))
    durable.save_thread(Thread(request_id="R-1", message_ids=("<a>",)))

    session = _session(settings, durable=durable)

    assert "R-1" in session.requests
    assert session.requests["R-1"].restored is True
    assert session.requests["R-1"].history is False


def test_demonstration_mode_still_hides_a_request_outside_its_focus(tmp_path: object) -> None:
    """The contrast: demonstration mode honours the (empty) request_ids and does
    NOT restore R-1 — the behaviour that lost the dashboard on deploy."""
    settings = _base_settings(tmp_path)  # demonstration mode (the default)
    DemonstrationFile(tmp_path).save(Demonstration(started_at=NOW, request_ids=()))
    durable = InMemoryStore()
    durable.save_request(_stored("R-1", RequestState.VALIDATED))
    durable.save_thread(Thread(request_id="R-1", message_ids=("<a>",)))

    session = _session(settings, durable=durable)

    assert session.requests == {}


def test_terminal_requests_return_as_hidden_history(tmp_path: object) -> None:
    """Req 2. A terminal request is visible as history — restored, flagged, kept
    out of the active list and the rate pass, surfaced under a separate key."""
    settings = _operations(_base_settings(tmp_path), since=NOW - timedelta(hours=1))
    durable = InMemoryStore()
    durable.save_request(_stored("R-done", RequestState.NO_ELIGIBLE_RATE))
    durable.save_thread(Thread(request_id="R-done", message_ids=("<t>",)))

    session = _session(settings, durable=durable)
    assert session.requests["R-done"].history is True

    snap = live_serialize.snapshot(session)
    assert all(r["request_id"] != "R-done" for r in snap["requests"])
    assert any(r["request_id"] == "R-done" for r in snap["history"])
    assert snap["demonstration"]["history"] == 1


def test_a_request_awaiting_approval_restores_as_validated(tmp_path: object) -> None:
    """Req C. The approval packet is never persisted, so a request found in a
    pre-send rate state is rewound to VALIDATED and its card re-derived by the
    next poll — never surfaced as an approval with no rates behind it."""
    settings = _operations(_base_settings(tmp_path), since=NOW - timedelta(hours=1))
    durable = InMemoryStore()
    for state in (RequestState.RATE_SELECTED, RequestState.PENDING_APPROVAL):
        durable.save_request(_stored(f"R-{state.value}", state))
        durable.save_thread(Thread(request_id=f"R-{state.value}", message_ids=("<x>",)))

    session = _session(settings, durable=durable)

    for state in (RequestState.RATE_SELECTED, RequestState.PENDING_APPROVAL):
        restored = session.requests[f"R-{state.value}"]
        assert restored.state is RequestState.VALIDATED
        assert restored.restored is True


def test_quotation_sent_restores_unchanged(tmp_path: object) -> None:
    """A quotation that already went out is committed and non-terminal: it comes
    back as QUOTATION_SENT (no card needed), awaiting the client's accept."""
    settings = _operations(_base_settings(tmp_path), since=NOW - timedelta(hours=1))
    durable = InMemoryStore()
    durable.save_request(_stored("R-sent", RequestState.QUOTATION_SENT))
    durable.save_thread(Thread(request_id="R-sent", message_ids=("<q>",)))

    session = _session(settings, durable=durable)

    restored = session.requests["R-sent"]
    assert restored.state is RequestState.QUOTATION_SENT
    assert restored.quotation_sent is True
    assert restored.history is False


# --- startup: no fresh demonstration, seeded cutoff, refusal --------------------


def test_build_operations_seeds_the_watermark_and_starts_no_demonstration(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Req 1 + 3. Operations startup does not start a demonstration; the mail
    cutoff is seeded from operations_since until the first poll writes one."""
    settings = _operations(_base_settings(tmp_path), since=NOW - timedelta(hours=2))
    monkeypatch.setattr(bootstrap, "build_gmail_email_sink", lambda _s: CollectingEmailSink())
    monkeypatch.setattr(bootstrap, "build_extractor", lambda _s: ScriptedExtractor())

    session = build_live_session(settings)

    assert session.operations_mode is True
    assert session.demonstration.started_at is None
    assert session.demonstration.last_poll_watermark == NOW - timedelta(hours=2)


def test_operations_refuses_to_start_without_a_cutoff(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Req 1 answer. No watermark and no operations_since: refuse, rather than
    silently defaulting the cutoff to now and skipping earlier mail."""
    settings = _operations(_base_settings(tmp_path), since=None)
    monkeypatch.setattr(bootstrap, "build_gmail_email_sink", lambda _s: CollectingEmailSink())
    monkeypatch.setattr(bootstrap, "build_extractor", lambda _s: ScriptedExtractor())

    with pytest.raises(PermanentFailure, match="OPERATIONS_SINCE"):
        build_live_session(settings)


def test_a_persisted_watermark_is_not_overwritten_by_operations_since(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seed is a first-boot fallback only: once a poll has recorded a
    watermark, that stands and operations_since is ignored."""
    settings = _operations(_base_settings(tmp_path), since=NOW - timedelta(hours=2))
    DemonstrationFile(tmp_path).save(Demonstration(last_poll_watermark=NOW - timedelta(minutes=5)))
    monkeypatch.setattr(bootstrap, "build_gmail_email_sink", lambda _s: CollectingEmailSink())
    monkeypatch.setattr(bootstrap, "build_extractor", lambda _s: ScriptedExtractor())

    session = build_live_session(settings)

    assert session.demonstration.last_poll_watermark == NOW - timedelta(minutes=5)


# --- the watermark never passes uncommitted work -------------------------------


def test_the_watermark_holds_at_the_oldest_uncommitted_message(tmp_path: object) -> None:
    """Req B. A message not recorded in a durable thread is uncommitted (a draft
    or a deferred reply); the cutoff must not pass it even when a newer message
    was committed, so a restart re-reads and re-derives it."""
    settings = _operations(_base_settings(tmp_path), since=NOW - timedelta(hours=1))
    durable = InMemoryStore()
    durable.save_thread(Thread(request_id="R-c", message_ids=("<committed>",)))
    session = _session(settings, durable=durable)
    session._demonstration.record_watermark(NOW - timedelta(hours=1))

    held = _email("<held>", NOW - timedelta(minutes=40))  # older, uncommitted
    committed = _email("<committed>", NOW - timedelta(minutes=10))  # newer, committed

    session._advance_watermark([committed, held])

    assert session.demonstration.last_poll_watermark == held.received_at


def test_the_watermark_advances_when_everything_is_committed(tmp_path: object) -> None:
    """With nothing held, the cutoff advances to the newest message handled, so
    the next poll's window starts from there."""
    settings = _operations(_base_settings(tmp_path), since=NOW - timedelta(hours=1))
    durable = InMemoryStore()
    durable.save_thread(Thread(request_id="R", message_ids=("<m1>", "<m2>")))
    session = _session(settings, durable=durable)
    session._demonstration.record_watermark(NOW - timedelta(hours=1))

    m1 = _email("<m1>", NOW - timedelta(minutes=30))
    m2 = _email("<m2>", NOW - timedelta(minutes=10))

    session._advance_watermark([m1, m2])

    assert session.demonstration.last_poll_watermark == m2.received_at


def test_operations_covers_admits_mail_older_than_any_started_at(tmp_path: object) -> None:
    """Operations scoping is the date-bounded fetch plus already-processed, not a
    started_at cutoff — so an old message the fetch returned is in scope."""
    settings = _operations(_base_settings(tmp_path), since=NOW - timedelta(hours=1))
    session = _session(settings, durable=InMemoryStore())

    assert session._covers(NOW - timedelta(days=30)) is True


def test_an_unapproved_clarification_draft_is_rederived_after_a_restart(tmp_path: object) -> None:
    """Req B, end to end. An enquiry that drafts a clarification commits nothing
    (NEEDS_INFO is uncommitted on purpose), so the watermark holds at its
    message and a restart re-reads that email and re-derives the draft rather
    than losing it. The whole workflow is real here — only the mailbox is a stub.
    """
    settings = _operations(_base_settings(tmp_path), since=None)

    first = LiveSession(
        settings,
        source=StubSource(ENQUIRY),
        sink=CollectingEmailSink(),
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION),
        clock=FixedClock(NOW),
    )
    first.poll()
    request_id = next(iter(first.requests))
    assert first.requests[request_id].state is RequestState.NEEDS_INFO
    assert first.requests[request_id].clarification is not None
    # The draft is uncommitted, and the watermark did not pass its message.
    assert JsonFileStore(tmp_path).get_request(request_id) is None
    assert first.demonstration.last_poll_watermark == ENQUIRY.received_at

    # A restart: nothing to restore from the store, but the poll re-reads the
    # enquiry (it is at/after the held watermark and was never committed) and
    # rebuilds the same draft.
    second = LiveSession(
        settings,
        source=StubSource(ENQUIRY),
        sink=CollectingEmailSink(),
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION),
        clock=FixedClock(NOW),
    )
    assert second.requests == {}, "an uncommitted draft is not in the store to restore"
    second.poll()

    assert second.requests[request_id].state is RequestState.NEEDS_INFO
    assert second.requests[request_id].clarification is not None


# --- a restore re-run is audited ------------------------------------------------


def test_a_restored_requests_reenqueue_is_audited_as_a_restart_rerun(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restored VALIDATED request whose (unpersisted) result is re-derived logs
    exactly one restart-rerun audit event, then clears the flag."""
    import translog_quote.interface.web.live_session as live_session

    settings = _operations(_base_settings(tmp_path), since=NOW - timedelta(hours=1))
    settings = settings.model_copy(
        update={
            "webcargo": settings.webcargo.model_copy(update={"mode": "browser"}),
            "goods_type": settings.goods_type.model_copy(
                update={"catalog": ("0000 - General Cargo",)}
            ),
        }
    )
    session = _session(settings, durable=InMemoryStore())
    monkeypatch.setattr(live_session, "enqueue_rate_search", lambda _job, _s: ("job-1", True))

    request = LiveRequest(
        request_id="R-1",
        client_address="client@example.com",
        state=RequestState.VALIDATED,
        record=FULL_RECORD,
        validation=validate_shipment(FULL_RECORD),
        operator_goods_type="0000 - General Cargo",
        operator_goods_type_fingerprint=record_fingerprint(
            "Engineering components", "general cargo", False
        ),
        operator_goods_type_by="ops",
        restored=True,
    )

    session._advance_browser_rate_search(request)

    rerun = [
        e for e in session.audit.events if e.event is AuditEventType.RATE_SEARCH_RERUN_AFTER_RESTART
    ]
    assert len(rerun) == 1
    assert rerun[0].request_id == "R-1"
    assert request.restored is False
