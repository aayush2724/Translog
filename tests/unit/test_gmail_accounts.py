"""The multi-account Gmail configuration model (Phase 1).

Configuration only — nothing here polls, sends, or persists; `resolve_gmail_accounts`
is not yet consumed by the runtime. These tests pin the model's contract:

- multiple account files load, sorted by filename and validated;
- a malformed account file fails loudly, naming the file (never silently skipped);
- a disabled account is parsed and carried (callers filter it later);
- with no accounts directory, exactly one `default` account is synthesised from
  the single-account `GmailSettings` — the backward-compatibility guarantee;
- two *live* accounts may not share an OAuth token file.

Every test writes its own JSON under pytest's `tmp_path`, with synthetic values
only, so it never touches a real credential or the developer's `.env`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from translog_quote.config import (
    GmailSettings,
    Settings,
    resolve_gmail_accounts,
)
from translog_quote.config.settings import DemoSettings


def _dir_settings(accounts_dir: Path) -> Settings:
    """Settings whose only relevant field is the accounts directory."""
    return Settings(gmail=GmailSettings(accounts_dir=accounts_dir))


def _write_account(directory: Path, name: str, **fields: object) -> Path:
    path = directory / name
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


# --- loading multiple account configs -------------------------------------------


def test_multiple_account_configs_load_sorted_and_validated(tmp_path: Path) -> None:
    _write_account(
        tmp_path,
        "beta.json",
        address="beta@example.com",
        read_token_path=".secrets/beta_read.json",
        send_token_path=".secrets/beta_send.json",
        approver_address="ops@example.com",
        query="in:inbox",
        enabled=True,
    )
    _write_account(
        tmp_path,
        "alpha.json",  # account_id omitted -> defaults to the file stem
        address="alpha@example.com",
        read_token_path=".secrets/alpha_read.json",
        send_token_path=".secrets/alpha_send.json",
    )

    accounts = resolve_gmail_accounts(_dir_settings(tmp_path))

    assert [a.account_id for a in accounts] == ["alpha", "beta"]  # sorted by filename
    assert accounts[0].address == "alpha@example.com"
    assert accounts[0].read_token_path == Path(".secrets/alpha_read.json")
    assert accounts[0].query == "in:inbox"  # default inherited
    assert accounts[1].approver_address == "ops@example.com"
    assert accounts[1].effective_sender == "beta@example.com"  # no sender_address override


def test_sender_address_overrides_the_from_address(tmp_path: Path) -> None:
    _write_account(
        tmp_path,
        "acct.json",
        address="reads@example.com",
        sender_address="sends@example.com",
        read_token_path=".secrets/r.json",
        send_token_path=".secrets/s.json",
    )
    (account,) = resolve_gmail_accounts(_dir_settings(tmp_path))
    assert account.effective_sender == "sends@example.com"


# --- malformed account config ---------------------------------------------------


def test_invalid_json_fails_loudly_naming_the_file(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="broken.json"):
        resolve_gmail_accounts(_dir_settings(tmp_path))


def test_an_unknown_field_is_rejected(tmp_path: Path) -> None:
    _write_account(
        tmp_path,
        "acct.json",
        address="a@example.com",
        read_token_path=".secrets/r.json",
        send_token_path=".secrets/s.json",
        surprise="not a real field",
    )
    with pytest.raises(ValueError, match="acct.json"):
        resolve_gmail_accounts(_dir_settings(tmp_path))


def test_a_missing_required_field_is_rejected(tmp_path: Path) -> None:
    _write_account(
        tmp_path,
        "acct.json",
        address="a@example.com",  # read_token_path / send_token_path missing
    )
    with pytest.raises(ValueError, match="acct.json"):
        resolve_gmail_accounts(_dir_settings(tmp_path))


def test_an_invalid_account_id_slug_is_rejected(tmp_path: Path) -> None:
    _write_account(
        tmp_path,
        "weird.json",
        account_id="../escape",
        address="a@example.com",
        read_token_path=".secrets/r.json",
        send_token_path=".secrets/s.json",
    )
    with pytest.raises(ValueError, match="weird.json"):
        resolve_gmail_accounts(_dir_settings(tmp_path))


def test_an_empty_accounts_dir_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No Gmail account files"):
        resolve_gmail_accounts(_dir_settings(tmp_path))


def test_accounts_dir_that_is_not_a_directory_is_rejected(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "accounts.json"
    not_a_dir.write_text("{}", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        resolve_gmail_accounts(_dir_settings(not_a_dir))


# --- disabled account -----------------------------------------------------------


def test_a_disabled_account_is_parsed_and_carried(tmp_path: Path) -> None:
    _write_account(
        tmp_path,
        "paused.json",
        address="paused@example.com",
        read_token_path=".secrets/p_read.json",
        send_token_path=".secrets/p_send.json",
        enabled=False,
    )
    accounts = resolve_gmail_accounts(_dir_settings(tmp_path))
    assert len(accounts) == 1
    assert accounts[0].account_id == "paused"
    assert accounts[0].enabled is False


# --- default single-account compatibility (no accounts directory) ----------------


def test_no_accounts_dir_synthesises_exactly_one_default_account() -> None:
    settings = Settings(
        gmail=GmailSettings(
            test_address="solo@example.com",
            token_path=Path(".secrets/gmail_token.json"),
            send_token_path=Path(".secrets/gmail_send_token.json"),
            approver_address="approver@example.com",
            query='in:inbox -subject:"[TRANSLOG INTERNAL]"',
            sender_address=None,
        ),
        demo=DemoSettings(),
    )

    accounts = resolve_gmail_accounts(settings)

    assert len(accounts) == 1
    only = accounts[0]
    assert only.account_id == "default"
    assert only.address == "solo@example.com"
    assert only.read_token_path == Path(".secrets/gmail_token.json")
    assert only.send_token_path == Path(".secrets/gmail_send_token.json")
    assert only.approver_address == "approver@example.com"
    assert only.query == 'in:inbox -subject:"[TRANSLOG INTERNAL]"'
    assert only.enabled is True
    assert only.effective_sender == "solo@example.com"  # falls back to address


def test_default_account_carries_the_operations_since_cutoff() -> None:
    from datetime import UTC, datetime

    settings = Settings(
        gmail=GmailSettings(test_address="solo@example.com"),
        demo=DemoSettings(operations_since=datetime(2026, 9, 16, tzinfo=UTC)),
    )
    (only,) = resolve_gmail_accounts(settings)
    assert only.operations_since == datetime(2026, 9, 16, tzinfo=UTC)


# --- token path collision validation --------------------------------------------


def test_two_live_accounts_may_not_share_a_read_token(tmp_path: Path) -> None:
    shared = ".secrets/shared_read.json"
    _write_account(
        tmp_path, "a.json", address="a@x", read_token_path=shared,
        send_token_path=".secrets/a_send.json",
    )
    _write_account(
        tmp_path, "b.json", address="b@x", read_token_path=shared,
        send_token_path=".secrets/b_send.json",
    )
    with pytest.raises(ValueError, match="share a read token"):
        resolve_gmail_accounts(_dir_settings(tmp_path))


def test_two_live_accounts_may_not_share_a_send_token(tmp_path: Path) -> None:
    shared = ".secrets/shared_send.json"
    _write_account(
        tmp_path, "a.json", address="a@x", read_token_path=".secrets/a_read.json",
        send_token_path=shared,
    )
    _write_account(
        tmp_path, "b.json", address="b@x", read_token_path=".secrets/b_read.json",
        send_token_path=shared,
    )
    with pytest.raises(ValueError, match="share a send token"):
        resolve_gmail_accounts(_dir_settings(tmp_path))


def test_a_disabled_account_may_share_a_token_since_it_is_inert(tmp_path: Path) -> None:
    shared = ".secrets/shared_read.json"
    _write_account(
        tmp_path, "live.json", address="a@x", read_token_path=shared,
        send_token_path=".secrets/a_send.json",
    )
    _write_account(
        tmp_path, "off.json", address="b@x", read_token_path=shared,
        send_token_path=".secrets/b_send.json", enabled=False,
    )
    accounts = resolve_gmail_accounts(_dir_settings(tmp_path))
    assert {a.account_id for a in accounts} == {"live", "off"}


def test_duplicate_account_id_is_rejected(tmp_path: Path) -> None:
    _write_account(
        tmp_path, "one.json", account_id="dup", address="a@x",
        read_token_path=".secrets/1r.json", send_token_path=".secrets/1s.json",
    )
    _write_account(
        tmp_path, "two.json", account_id="dup", address="b@x",
        read_token_path=".secrets/2r.json", send_token_path=".secrets/2s.json",
    )
    with pytest.raises(ValueError, match="Duplicate Gmail account_id"):
        resolve_gmail_accounts(_dir_settings(tmp_path))
