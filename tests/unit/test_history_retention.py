"""The dashboard's History window.

A settled request — quotation sent, declined, closed with no rates, or resolved
by a person — moves under History rather than vanishing, and stays there for
``HISTORY_RETENTION`` measured from the instant it settled. After that it leaves
the desk: not in Active, not in History, and not as a selected detail. The
durable store and the audit trail keep it regardless; a late client reply still
correlates to it there.

The settle instant is persisted (``QuotationRequest.settled_at``) so a restart
neither resets the window nor resurrects last week's work. A terminal request
from a store written before the field existed has no instant and is treated as
settled long ago — hidden, never shown for an hour after every deploy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.unit.test_gmail_thread import ScriptedExtractor, StubSource

from translog_quote.adapters.clock import FixedClock
from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.store import InMemoryStore
from translog_quote.config import Settings
from translog_quote.domain.conversation import Thread
from translog_quote.domain.shipment import CargoDimensions, RequestSource, ShipmentRecord
from translog_quote.domain.workflow import QuotationRequest, RequestState
from translog_quote.interface.web import live_serialize
from translog_quote.interface.web.live_serialize import HISTORY_RETENTION
from translog_quote.interface.web.live_session import LiveSession
from translog_quote.interface.web.multi_account_session import MultiAccountSession

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
APPROVER = "ops@translog.example"
CLIENT = "client@example.com"
ONE_SECOND = timedelta(seconds=1)

RECORD = ShipmentRecord(
    request_id="R-1",
    source=RequestSource.EMAIL,
    origin="Ahmedabad",
    destination="Bahrain",
    weight_kg=500.0,
    dimensions_in=CargoDimensions(length=34, width=24, height=6),
)


def _settings(state_dir: object) -> Settings:
    """Operations mode (what Render runs): a restart restores from the store."""
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    return base.model_copy(
        update={
            "openrouter": base.openrouter.model_copy(update={"api_key": "k"}),
            "demo": base.demo.model_copy(
                update={
                    "state_dir": state_dir,
                    "startup_mode": "operations",
                    "operations_since": NOW - timedelta(hours=1),
                }
            ),
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


def _session(
    settings: Settings, *, durable: InMemoryStore, sink: CollectingEmailSink | None = None
) -> LiveSession:
    return LiveSession(
        settings,
        source=StubSource(),
        sink=sink or CollectingEmailSink(),
        extractor=ScriptedExtractor(),
        durable=durable,
        clock=FixedClock(NOW),
    )


def _stored(request_id: str, state: RequestState, **fields: object) -> QuotationRequest:
    return QuotationRequest(
        request_id=request_id,
        state=state,
        record=RECORD.model_copy(update={"request_id": request_id}),
        client_address=CLIENT,
        **fields,  # type: ignore[arg-type]
    )


def _restored(tmp_path: object, *stored: QuotationRequest) -> tuple[LiveSession, InMemoryStore]:
    durable = InMemoryStore()
    for request in stored:
        durable.save_request(request)
        durable.save_thread(Thread(request_id=request.request_id, message_ids=("<m>",)))
    return _session(_settings(tmp_path), durable=durable), durable


def _ids(rows: object) -> list[str]:
    assert isinstance(rows, list)
    return [str(row["request_id"]) for row in rows]


def _history_count(snap: object) -> int:
    assert isinstance(snap, dict)
    demo = snap["demonstration"]
    assert isinstance(demo, dict)
    return int(demo["history"])


# --- the window ------------------------------------------------------------------


def test_the_history_window_is_one_hour() -> None:
    """The business number, pinned: an hour after it settles a request leaves
    the desk. Change this constant deliberately, not by accident."""
    assert HISTORY_RETENTION.total_seconds() == 3600


def test_a_settled_request_shows_under_history_until_the_window_passes(tmp_path: object) -> None:
    settled = NOW - timedelta(minutes=30)
    session, durable = _restored(
        tmp_path, _stored("R-done", RequestState.CLOSED_NO_RATES, settled_at=settled)
    )

    inside = live_serialize.snapshot(session)
    assert _ids(inside["requests"]) == []
    assert _ids(inside["history"]) == ["R-done"]
    assert _history_count(inside) == 1

    # Exclusive boundary: one second short of the hour it is still there ...
    last_moment = live_serialize.snapshot(session, now=settled + HISTORY_RETENTION - ONE_SECOND)
    assert _ids(last_moment["history"]) == ["R-done"]

    # ... and at exactly the hour it has left the desk entirely.
    after = live_serialize.snapshot(session, now=settled + HISTORY_RETENTION)
    assert _ids(after["requests"]) == []
    assert _ids(after["history"]) == []
    assert _history_count(after) == 0

    # Off the desk is not deleted: the store and the session still hold it, so a
    # late reply correlates and a restart cannot re-derive it as new work.
    assert durable.get_request("R-done") is not None
    assert "R-done" in session.requests


def test_a_settled_request_the_desk_never_timed_is_treated_as_old(tmp_path: object) -> None:
    """A store written before ``settled_at`` existed: its terminal requests have
    no settle instant. They settled before this build — long ago by any reading
    — so they are hidden rather than shown for an hour after every deploy."""
    session, durable = _restored(
        tmp_path,
        _stored("R-quoted", RequestState.QUOTATION_SENT),
        _stored("R-declined", RequestState.MAKER_REJECTED),
        _stored("R-failed", RequestState.FAILED),
    )

    snap = live_serialize.snapshot(session)

    assert _ids(snap["requests"]) == []
    assert _ids(snap["history"]) == []
    assert _history_count(snap) == 0
    assert {r.request_id for r in durable.all_requests()} == {"R-quoted", "R-declined", "R-failed"}


def test_a_resolved_request_from_an_older_build_uses_its_resolved_time(tmp_path: object) -> None:
    """Manual-review resolution persisted ``resolved_at`` before ``settled_at``
    existed. Same instant, older field: it starts the window all the same."""
    session, _ = _restored(
        tmp_path,
        _stored(
            "R-resolved",
            RequestState.RESOLVED,
            resolved_by="ops",
            resolved_at=NOW - timedelta(minutes=10),
        ),
    )

    assert _ids(live_serialize.snapshot(session)["history"]) == ["R-resolved"]
    assert _ids(live_serialize.snapshot(session, now=NOW + timedelta(minutes=50))["history"]) == []


def test_active_requests_have_no_age_limit(tmp_path: object) -> None:
    """Only settled work ages out. A request still in play — here one handed to
    a person and one awaiting a client — stays on the desk however old."""
    session, _ = _restored(
        tmp_path,
        _stored("R-review", RequestState.MANUAL_REVIEW),
        _stored("R-waiting", RequestState.CLARIFICATION_SENT),
    )

    snap = live_serialize.snapshot(session, now=NOW + timedelta(days=30))

    assert set(_ids(snap["requests"])) == {"R-review", "R-waiting"}
    assert _ids(snap["history"]) == []


# --- the detail pane -------------------------------------------------------------


def test_a_selected_request_past_its_window_has_no_detail_either(tmp_path: object) -> None:
    """Off the desk means off the detail pane too: the page must never keep
    showing a request that has no card anywhere."""
    settled = NOW - timedelta(minutes=59)
    session, _ = _restored(
        tmp_path, _stored("R-done", RequestState.QUOTATION_SENT, settled_at=settled)
    )

    inside = live_serialize.snapshot(session, selected="R-done")
    assert isinstance(inside["selected"], dict)
    assert inside["selected"]["request_id"] == "R-done"

    after = live_serialize.snapshot(session, selected="R-done", now=NOW + timedelta(minutes=1))
    assert after["selected"] is None


# --- the settle instant is stamped where the request settles --------------------


def test_resolving_manual_review_stamps_and_persists_the_settle_time(tmp_path: object) -> None:
    session, durable = _restored(tmp_path, _stored("R-review", RequestState.MANUAL_REVIEW))

    session.resolve_manual_review("R-review", by="ops", note="priced by hand")

    live = session.requests["R-review"]
    assert live.state is RequestState.RESOLVED
    assert live.settled_at == NOW
    persisted = durable.get_request("R-review")
    assert persisted is not None
    assert persisted.state is RequestState.RESOLVED
    assert persisted.settled_at == NOW  # survives a restart

    assert _ids(live_serialize.snapshot(session)["history"]) == ["R-review"]
    assert _ids(live_serialize.snapshot(session, now=NOW + HISTORY_RETENTION)["history"]) == []


def test_the_no_rates_close_stamps_and_persists_the_settle_time(tmp_path: object) -> None:
    sink = CollectingEmailSink()
    durable = InMemoryStore()
    durable.save_request(_stored("R-1", RequestState.VALIDATED))
    durable.save_thread(Thread(request_id="R-1", message_ids=("<m>",)))
    session = _session(_settings(tmp_path), durable=durable, sink=sink)

    session._notify_no_rates(session.requests["R-1"])  # noqa: SLF001 - the close itself

    assert [m.to_address for m in sink.sent] == [CLIENT]
    live = session.requests["R-1"]
    assert live.state is RequestState.CLOSED_NO_RATES
    assert live.settled_at == NOW
    persisted = durable.get_request("R-1")
    assert persisted is not None
    assert persisted.settled_at == NOW


def test_the_settle_time_is_set_once_and_never_moves(tmp_path: object) -> None:
    """A replayed settling transition must not push a finished request back
    onto the desk by re-stamping it with a later instant."""
    first = NOW - timedelta(minutes=45)
    session, durable = _restored(
        tmp_path, _stored("R-done", RequestState.MAKER_REJECTED, settled_at=first)
    )

    session._mark_settled(session.requests["R-done"])  # noqa: SLF001 - the stamp itself

    assert session.requests["R-done"].settled_at == first
    persisted = durable.get_request("R-done")
    assert persisted is not None
    assert persisted.settled_at == first


def test_an_unsettled_request_is_never_stamped(tmp_path: object) -> None:
    session, durable = _restored(tmp_path, _stored("R-open", RequestState.VALIDATED))

    session._mark_settled(session.requests["R-open"])  # noqa: SLF001 - the stamp itself

    assert session.requests["R-open"].settled_at is None
    persisted = durable.get_request("R-open")
    assert persisted is not None
    assert persisted.settled_at is None


# --- persistence shape -----------------------------------------------------------


def test_the_settle_time_round_trips_through_the_stores_json() -> None:
    """Both durable stores write ``model_dump_json`` and read
    ``model_validate_json``; the instant must come back timezone-aware, and a
    record written before the field existed must still load (as None)."""
    settled = NOW - timedelta(minutes=5)
    stored = _stored("R-1", RequestState.QUOTATION_SENT, settled_at=settled)

    reloaded = QuotationRequest.model_validate_json(stored.model_dump_json())
    assert reloaded.settled_at == settled
    assert reloaded.settled_at is not None and reloaded.settled_at.tzinfo is not None

    older = _stored("R-2", RequestState.QUOTATION_SENT).model_dump_json()
    assert '"settled_at":null' in older
    assert QuotationRequest.model_validate_json(older).settled_at is None


# --- several accounts ------------------------------------------------------------


def test_the_window_applies_per_account_in_the_unified_view(tmp_path: object) -> None:
    alpha, _ = _restored(
        Path(str(tmp_path)) / "alpha",
        _stored("alpha:R-1", RequestState.QUOTATION_SENT, settled_at=NOW - timedelta(minutes=5)),
    )
    beta, _ = _restored(
        Path(str(tmp_path)) / "beta",
        _stored("beta:R-1", RequestState.QUOTATION_SENT, settled_at=NOW - timedelta(hours=2)),
    )
    multi = MultiAccountSession(_settings(tmp_path), {"alpha": alpha, "beta": beta})

    snap = live_serialize.snapshot(multi, selected="beta:R-1")

    assert _ids(snap["history"]) == ["alpha:R-1"]
    assert _history_count(snap) == 1
    assert snap["selected"] is None  # beta's settled two hours ago: no card, no detail

    shown = live_serialize.snapshot(multi, selected="alpha:R-1")
    assert isinstance(shown["selected"], dict)
    assert shown["selected"]["account"] == "alpha"

    later = live_serialize.snapshot(multi, now=NOW + HISTORY_RETENTION)
    assert _ids(later["history"]) == []

