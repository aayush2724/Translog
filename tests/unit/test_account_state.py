"""Per-account state isolation (Phase 2, steps 1-3).

The storage classes are already directory-parameterised, so isolation is a matter
of *where* each account's state lives. These tests pin that lever:

- `account_state_dir` resolves to the historical root in single-account mode and
  to `state_dir/accounts/<id>` in multi-account mode, one distinct directory per
  account, and refuses an unsafe id;
- the persistence builders honour that — root when `account_id` is omitted
  (today's behaviour), the account's own directory when given;
- existing single-account state still loads through the omitted-account builders;
- two accounts never see each other's state;
- `reset-state` clears the right directory in each mode, still by exact filename.

Every test works under pytest's `tmp_path`; nothing touches the real state dir.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from translog_quote import bootstrap
from translog_quote.config import Settings
from translog_quote.config.settings import DemoSettings, GmailSettings
from translog_quote.domain.conversation import Thread
from translog_quote.interface.demo.reset_state import EXIT_OK, REMOVABLE, run_reset_state
from translog_quote.interface.web.audit_log import build_audit_log
from translog_quote.interface.web.demonstration import Demonstration, build_demonstration


def _settings(tmp_path: Path, *, multi: bool) -> Settings:
    """Settings with an isolated state dir; multi-account when asked."""
    gmail_kwargs: dict[str, object] = {}
    if multi:
        gmail_kwargs["accounts_dir"] = tmp_path / "config"  # where account files live
    return Settings(
        gmail=GmailSettings(**gmail_kwargs),  # type: ignore[arg-type]
        demo=DemoSettings(state_dir=tmp_path / "state"),
    )


def _write_account_config(settings: Settings, account_id: str) -> None:
    """A minimal valid account config file (config lives under accounts_dir; its
    state lives elsewhere, under state_dir/accounts/<id>)."""
    accounts_dir = settings.gmail.accounts_dir
    assert accounts_dir is not None
    accounts_dir.mkdir(parents=True, exist_ok=True)
    (accounts_dir / f"{account_id}.json").write_text(
        json.dumps(
            {
                "address": f"{account_id}@example.com",
                "read_token_path": f".secrets/{account_id}_read.json",
                "send_token_path": f".secrets/{account_id}_send.json",
            }
        ),
        encoding="utf-8",
    )


# --- account_state_dir ----------------------------------------------------------


def test_account_state_dir_single_account_mode_is_the_root(tmp_path: Path) -> None:
    settings = _settings(tmp_path, multi=False)
    assert bootstrap.account_state_dir(settings, "default") == settings.demo.state_dir


def test_account_state_dir_multi_account_mode_nests_under_accounts(tmp_path: Path) -> None:
    settings = _settings(tmp_path, multi=True)
    assert bootstrap.account_state_dir(settings, "sales-in") == (
        settings.demo.state_dir / "accounts" / "sales-in"
    )


def test_distinct_accounts_get_distinct_directories(tmp_path: Path) -> None:
    settings = _settings(tmp_path, multi=True)
    a = bootstrap.account_state_dir(settings, "a")
    b = bootstrap.account_state_dir(settings, "b")
    assert a != b
    assert a.parent == b.parent == settings.demo.state_dir / "accounts"


@pytest.mark.parametrize("unsafe", ["../evil", "a/b", "..", "", "/abs"])
def test_an_unsafe_account_id_is_refused(tmp_path: Path, unsafe: str) -> None:
    settings = _settings(tmp_path, multi=True)
    with pytest.raises(ValueError, match="not a safe slug"):
        bootstrap.account_state_dir(settings, unsafe)


# --- builders: root vs subdirectory ---------------------------------------------


def test_store_builder_root_and_subdirectory(tmp_path: Path) -> None:
    settings = _settings(tmp_path, multi=True)
    root = bootstrap.build_persistent_store(settings)
    scoped = bootstrap.build_persistent_store(settings, account_id="acct")
    assert root.directory == settings.demo.state_dir  # type: ignore[attr-defined]
    assert scoped.directory == settings.demo.state_dir / "accounts" / "acct"  # type: ignore[attr-defined]


def test_audit_builder_root_and_subdirectory(tmp_path: Path) -> None:
    settings = _settings(tmp_path, multi=True)
    root = build_audit_log(settings)
    scoped = build_audit_log(settings, account_id="acct")
    assert root.path == settings.demo.state_dir / "audit.jsonl"
    assert scoped.path == settings.demo.state_dir / "accounts" / "acct" / "audit.jsonl"


def test_demonstration_builder_root_and_subdirectory(tmp_path: Path) -> None:
    settings = _settings(tmp_path, multi=True)
    root = build_demonstration(settings)
    scoped = build_demonstration(settings, account_id="acct")
    assert root.path == settings.demo.state_dir / "demonstration.json"
    assert scoped.path == settings.demo.state_dir / "accounts" / "acct" / "demonstration.json"


def test_omitting_account_id_preserves_todays_root_layout(tmp_path: Path) -> None:
    """In single-account mode, omitting account_id must be byte-for-byte today's
    behaviour: everything under the state_dir root."""
    settings = _settings(tmp_path, multi=False)
    assert bootstrap.build_persistent_store(settings).directory == settings.demo.state_dir  # type: ignore[attr-defined]
    assert build_audit_log(settings).path == settings.demo.state_dir / "audit.jsonl"
    assert (
        build_demonstration(settings).path
        == settings.demo.state_dir / "demonstration.json"
    )


# --- existing state remains readable --------------------------------------------


def test_existing_root_state_is_still_readable(tmp_path: Path) -> None:
    settings = _settings(tmp_path, multi=False)
    state = settings.demo.state_dir
    state.mkdir(parents=True, exist_ok=True)

    thread = Thread(request_id="R-1", message_ids=("m1@x",))
    (state / "threads.json").write_text(
        json.dumps({"R-1": thread.model_dump(mode="json")}), encoding="utf-8"
    )
    watermark = datetime(2026, 9, 16, tzinfo=UTC)
    (state / "demonstration.json").write_text(
        Demonstration(last_poll_watermark=watermark).model_dump_json(), encoding="utf-8"
    )

    store = bootstrap.build_persistent_store(settings)  # omitted account_id -> root
    demo = build_demonstration(settings)
    assert [t.request_id for t in store.all_threads()] == ["R-1"]
    assert demo.current.last_poll_watermark == watermark


# --- no accidental cross-account state visibility --------------------------------


def test_two_accounts_never_see_each_others_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path, multi=True)

    store_a = bootstrap.build_persistent_store(settings, account_id="a")
    store_a.save_thread(Thread(request_id="R-A", message_ids=("mA@x",)))

    store_b = bootstrap.build_persistent_store(settings, account_id="b")
    assert store_b.all_threads() == ()  # b's directory is its own and empty

    # a's write really landed in a's directory, and reload proves it persisted there.
    reopened_a = bootstrap.build_persistent_store(settings, account_id="a")
    assert [t.request_id for t in reopened_a.all_threads()] == ["R-A"]
    assert (settings.demo.state_dir / "accounts" / "a" / "threads.json").exists()
    assert not (settings.demo.state_dir / "accounts" / "b" / "threads.json").exists()


# --- reset-state ----------------------------------------------------------------


def _seed(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in REMOVABLE:
        (directory / name).write_text("{}", encoding="utf-8")


def test_reset_state_single_account_clears_the_root(tmp_path: Path) -> None:
    settings = _settings(tmp_path, multi=False)
    _seed(settings.demo.state_dir)

    code = run_reset_state(settings=settings, confirmed=True, out=io.StringIO())

    assert code == EXIT_OK
    assert [p.name for p in settings.demo.state_dir.iterdir()] == []


def test_reset_state_multi_account_clears_each_account_dir(tmp_path: Path) -> None:
    settings = _settings(tmp_path, multi=True)
    _write_account_config(settings, "a")
    _write_account_config(settings, "b")
    dir_a = bootstrap.account_state_dir(settings, "a")
    dir_b = bootstrap.account_state_dir(settings, "b")
    _seed(dir_a)
    _seed(dir_b)
    # a stray file at the (unused) root must be left alone in multi-account mode.
    settings.demo.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.demo.state_dir / "keep.txt").write_text("keep", encoding="utf-8")

    code = run_reset_state(settings=settings, confirmed=True, out=io.StringIO())

    assert code == EXIT_OK
    assert [p.name for p in dir_a.iterdir()] == []
    assert [p.name for p in dir_b.iterdir()] == []
    assert (settings.demo.state_dir / "keep.txt").exists()  # root untouched
