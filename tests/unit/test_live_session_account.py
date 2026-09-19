"""LiveSession as a single-account session (Phase 3b).

`LiveSession(account=...)` wires its Gmail source/sink and its state directory to
that one account; `LiveSession()` (no account) is byte-for-byte today's
single-account session. These tests capture the `account` the session hands to
the Phase 3a builders (so no token file or network is needed) and check the
per-account state directory via the real store/audit/demonstration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from translog_quote import bootstrap
from translog_quote.config import GmailAccount, Settings
from translog_quote.config.settings import DemoSettings, GmailSettings
from translog_quote.interface.web.live_session import LiveSession


class _FakeSource:
    def fetch_new(self, *, since: Any = None) -> tuple[Any, ...]:
        return ()


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the account passed to each Gmail builder; return fakes so no
    real transport/credential is built."""
    seen: dict[str, Any] = {}

    def fake_sink(settings: Settings, *, account: GmailAccount | None = None) -> object:
        seen["sink_account"] = account
        return object()

    def fake_source(
        settings: Settings, *, account: GmailAccount | None = None, **_: Any
    ) -> _FakeSource:
        seen["source_account"] = account
        return _FakeSource()

    monkeypatch.setattr(bootstrap, "build_gmail_email_sink", fake_sink)
    monkeypatch.setattr(bootstrap, "build_gmail_email_source", fake_source)
    return seen


def _account() -> GmailAccount:
    return GmailAccount(
        account_id="sales-in",
        address="sales@example.com",
        read_token_path=Path(".secrets/sales_read.json"),
        send_token_path=Path(".secrets/sales_send.json"),
        query="in:inbox SALES",
        approver_address="ops@example.com",
    )


def test_account_session_uses_account_source_sink_and_state_dir(
    tmp_path: Path, captured: dict[str, Any]
) -> None:
    account = _account()
    settings = Settings(
        gmail=GmailSettings(
            accounts_dir=tmp_path / "config",  # multi-account mode -> nested state
            test_address="solo@example.com",
            approver_address="ops@example.com",
        ),
        demo=DemoSettings(state_dir=tmp_path / "state"),
    )

    session = LiveSession(settings, account=account, extractor=object())  # type: ignore[arg-type]

    # identity retained (so a later aggregator can tell which mailbox owns a request)
    assert session.account is account

    # sink built for the account; source built for the account on first fetch
    assert captured["sink_account"] is account
    session._fetch(since=None)  # triggers the lazy source build
    assert captured["source_account"] is account

    # per-account state directory (Phase 2 layout)
    account_dir = tmp_path / "state" / "accounts" / "sales-in"
    assert session._durable.directory == account_dir  # type: ignore[attr-defined]
    assert session.audit.path == account_dir / "audit.jsonl"  # type: ignore[union-attr]
    assert session._demonstration.path == account_dir / "demonstration.json"


def test_no_account_session_preserves_root_state_and_settings(
    tmp_path: Path, captured: dict[str, Any]
) -> None:
    settings = Settings(
        gmail=GmailSettings(test_address="solo@example.com", approver_address="ops@example.com"),
        demo=DemoSettings(state_dir=tmp_path / "state"),
    )

    session = LiveSession(settings, extractor=object())  # type: ignore[arg-type]

    assert session.account is None
    assert captured["sink_account"] is None
    session._fetch(since=None)
    assert captured["source_account"] is None

    # root state directory, exactly as before
    root = tmp_path / "state"
    assert session._durable.directory == root  # type: ignore[attr-defined]
    assert session.audit.path == root / "audit.jsonl"  # type: ignore[union-attr]
    assert session._demonstration.path == root / "demonstration.json"
