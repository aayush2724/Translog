"""MultiAccountSession — many LiveSessions presented as one (Phase 3c).

Covers: namespaced request ids; one session per enabled account; merged
requests; per-owner routing of decide/approve/goods-type; poll isolation;
unified snapshot tagged by account; and build_live_session's single- vs
multi-account branch. Builders are monkeypatched so no token/network is used.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from translog_quote import bootstrap
from translog_quote.config import Settings
from translog_quote.config.settings import DemoSettings, GmailSettings, OpenRouterSettings
from translog_quote.domain.email import RawEmail
from translog_quote.domain.shipment import RequestSource, ShipmentRecord
from translog_quote.domain.validation import ValidationResult
from translog_quote.domain.workflow import RequestState
from translog_quote.interface.demo.gmail_thread import _request_id_for
from translog_quote.interface.web import live_serialize
from translog_quote.interface.web.live_session import (
    LiveRequest,
    LiveSession,
    _namespaced_request_id_for,
    build_live_session,
)
from translog_quote.interface.web.multi_account_session import MultiAccountSession


class _FakeSource:
    def fetch_new(self, *, since: Any = None) -> tuple[Any, ...]:
        return ()


@pytest.fixture(autouse=True)
def offline_builders(monkeypatch: pytest.MonkeyPatch) -> None:
    """No OAuth token, no network, no live model in any session built here."""
    monkeypatch.setattr(bootstrap, "build_extractor", lambda settings: object())
    monkeypatch.setattr(
        bootstrap, "build_gmail_email_sink", lambda settings, *, account=None: object()
    )
    monkeypatch.setattr(
        bootstrap,
        "build_gmail_email_source",
        lambda settings, *, account=None, **_: _FakeSource(),
    )


def _write_accounts(accounts_dir: Path, *specs: tuple[str, bool]) -> None:
    accounts_dir.mkdir(parents=True, exist_ok=True)
    for account_id, enabled in specs:
        (accounts_dir / f"{account_id}.json").write_text(
            json.dumps(
                {
                    "address": f"{account_id}@example.com",
                    "read_token_path": f".secrets/{account_id}_r.json",
                    "send_token_path": f".secrets/{account_id}_s.json",
                    "approver_address": "ops@example.com",
                    "enabled": enabled,
                }
            ),
            encoding="utf-8",
        )


def _multi_settings(tmp_path: Path, *specs: tuple[str, bool]) -> Settings:
    accounts_dir = tmp_path / "config"
    _write_accounts(accounts_dir, *specs)
    return Settings(
        openrouter=OpenRouterSettings(api_key="k"),  # type: ignore[arg-type]
        gmail=GmailSettings(
            accounts_dir=accounts_dir,
            test_address="solo@example.com",
            approver_address="ops@example.com",
            send_enabled=True,
        ),
        demo=DemoSettings(state_dir=tmp_path / "state"),
    )


def _single_settings(tmp_path: Path) -> Settings:
    return Settings(
        openrouter=OpenRouterSettings(api_key="k"),  # type: ignore[arg-type]
        gmail=GmailSettings(
            test_address="solo@example.com", approver_address="ops@example.com", send_enabled=True
        ),
        demo=DemoSettings(state_dir=tmp_path / "state"),
    )


def _live_request(request_id: str) -> LiveRequest:
    return LiveRequest(
        request_id=request_id,
        client_address="client@example.com",
        state=RequestState.EXTRACTED,
        record=ShipmentRecord(request_id=request_id, source=RequestSource.EMAIL),
        validation=ValidationResult(),
    )


# --- namespaced request ids -----------------------------------------------------


def test_namespaced_request_id_prefixes_the_account() -> None:
    email = RawEmail(
        message_id="<m1@x>",
        from_address="c@example.com",
        subject="s",
        body_text="b",
        received_at=datetime.datetime(2026, 9, 20, tzinfo=datetime.UTC),
    )
    factory = _namespaced_request_id_for("sales-in")
    assert factory(email) == "sales-in:" + _request_id_for(email)
    # single-account keeps the unprefixed id (no ':')
    assert ":" not in _request_id_for(email)


# --- build / composition --------------------------------------------------------


def test_build_creates_one_session_per_enabled_account(tmp_path: Path) -> None:
    settings = _multi_settings(tmp_path, ("alpha", True), ("beta", True), ("paused", False))
    ms = MultiAccountSession.build(settings)
    assert set(ms.sessions) == {"alpha", "beta"}  # disabled account excluded
    assert all(isinstance(s, LiveSession) for s in ms.sessions.values())
    assert ms.sessions["alpha"].account is not None
    assert ms.sessions["alpha"].account.account_id == "alpha"


def test_requests_merge_with_namespaced_keys(tmp_path: Path) -> None:
    settings = _multi_settings(tmp_path, ("alpha", True), ("beta", True))
    ms = MultiAccountSession.build(settings)
    ms.sessions["alpha"].requests["alpha:R-1"] = _live_request("alpha:R-1")
    ms.sessions["beta"].requests["beta:R-1"] = _live_request("beta:R-1")
    assert set(ms.requests) == {"alpha:R-1", "beta:R-1"}


# --- routing --------------------------------------------------------------------


def test_decide_routes_to_the_owning_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _multi_settings(tmp_path, ("alpha", True), ("beta", True))
    ms = MultiAccountSession.build(settings)
    calls: dict[str, tuple[str, str]] = {}
    for account_id, sess in ms.sessions.items():

        def record(
            request_id: str, *, choice: str, by: str, reason: str = "", _a: str = account_id
        ) -> object:
            calls["decide"] = (_a, request_id)
            return object()

        monkeypatch.setattr(sess, "decide", record)

    ms.decide("beta:R-9", choice="approve", by="op")
    assert calls["decide"] == ("beta", "beta:R-9")


def test_approve_and_goods_type_route_to_the_owning_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _multi_settings(tmp_path, ("alpha", True), ("beta", True))
    ms = MultiAccountSession.build(settings)
    seen: dict[str, str] = {}
    for account_id, sess in ms.sessions.items():
        monkeypatch.setattr(
            sess,
            "approve_clarification",
            lambda *, by, request_id, _a=account_id: seen.__setitem__("approve", _a),
        )
        monkeypatch.setattr(
            sess,
            "decide_goods_type",
            lambda request_id, *, goods_type, by, _a=account_id: seen.__setitem__("goods", _a),
        )

    ms.approve_clarification(by="op", request_id="alpha:R-2")
    ms.decide_goods_type("beta:R-3", goods_type="0000 - General Cargo", by="op")
    assert seen == {"approve": "alpha", "goods": "beta"}


def test_approve_without_request_id_is_refused(tmp_path: Path) -> None:
    from translog_quote.interface.web.live_session import LiveSequenceError

    settings = _multi_settings(tmp_path, ("alpha", True), ("beta", True))
    ms = MultiAccountSession.build(settings)
    with pytest.raises(LiveSequenceError):
        ms.approve_clarification(by="op")


# --- poll isolation -------------------------------------------------------------


def test_one_accounts_failed_poll_does_not_stop_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _multi_settings(tmp_path, ("alpha", True), ("beta", True))
    ms = MultiAccountSession.build(settings)
    polled: list[str] = []

    def boom() -> None:
        raise RuntimeError("token expired")

    monkeypatch.setattr(ms.sessions["alpha"], "poll", boom)
    monkeypatch.setattr(ms.sessions["beta"], "poll", lambda: polled.append("beta"))

    ms.poll()

    assert polled == ["beta"]  # beta still polled
    assert ms.sessions["alpha"].last_poll_error == "RuntimeError"
    assert ms.last_poll_error == "RuntimeError"  # aggregated


# --- unified snapshot -----------------------------------------------------------


def test_unified_snapshot_renders_both_accounts_tagged(tmp_path: Path) -> None:
    settings = _multi_settings(tmp_path, ("alpha", True), ("beta", True))
    ms = MultiAccountSession.build(settings)
    ms.sessions["alpha"].requests["alpha:R-1"] = _live_request("alpha:R-1")
    ms.sessions["beta"].requests["beta:R-1"] = _live_request("beta:R-1")

    snap = live_serialize.snapshot(ms)

    rows = list(snap["requests"]) + list(snap["history"])  # type: ignore[operator]
    accounts = {row["account"] for row in rows}  # type: ignore[index]
    ids = {row["request_id"] for row in rows}  # type: ignore[index]
    assert {"alpha", "beta"} <= accounts
    assert {"alpha:R-1", "beta:R-1"} <= ids
    assert snap["mode"]["approver_address"] == "ops@example.com"  # type: ignore[index]


# --- build_live_session branch --------------------------------------------------


def test_build_live_session_single_mode_returns_a_live_session(tmp_path: Path) -> None:
    session = build_live_session(_single_settings(tmp_path))
    assert isinstance(session, LiveSession)


def test_build_live_session_multi_mode_returns_a_multi_account_session(tmp_path: Path) -> None:
    settings = _multi_settings(tmp_path, ("alpha", True), ("beta", True))
    session = build_live_session(settings)
    assert isinstance(session, MultiAccountSession)
    assert set(session.sessions) == {"alpha", "beta"}
