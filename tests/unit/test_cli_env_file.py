"""`--env-file` on both entry points.

The flag is how an operator points a demonstration at a particular Gmail
account. Two things about it have to hold, and neither is obvious from the
argument parser:

- it must be removable from anywhere in the argument list, so
  `--env-file X gmail-auth` still runs `gmail-auth` and not the scenario demo
  named "--env-file" — `gmail-auth` is precisely the command that writes an
  OAuth token, so mis-dispatching it is how a token lands in the wrong place;
- a file that is not there must stop the command, not start it against the
  base configuration.

Nothing here touches a mailbox: the commands are stubbed and only the argument
handling around them runs.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from translog_quote.config import ENV_FILE_VAR
from translog_quote.interface.demo import __main__ as demo_main
from translog_quote.interface.web import __main__ as web_main

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".env.account"
    path.write_text("TRANSLOG_GMAIL__TEST_ADDRESS=second@example.com\n", encoding="utf-8")
    monkeypatch.delenv(ENV_FILE_VAR, raising=False)
    return path


# --- demo CLI -------------------------------------------------------------------


def test_the_flag_is_consumed_and_the_subcommand_still_dispatches(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    monkeypatch.setattr(demo_main, "run_gmail_auth", lambda: called.append("auth") or 0)

    exit_code = demo_main.main(["--env-file", str(env_file), "gmail-auth"])

    assert exit_code == 0
    assert called == ["auth"]
    assert os.environ[ENV_FILE_VAR] == str(env_file)


def test_the_flag_may_follow_the_subcommand_too(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    monkeypatch.setattr(demo_main, "run_gmail_auth_send", lambda: called.append("send") or 0)

    assert demo_main.main(["gmail-auth-send", "--env-file", str(env_file)]) == 0
    assert called == ["send"]


def test_other_flags_survive_the_removal(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--approved-by` is read from the same list the flag was cut out of."""
    seen: list[str | None] = []
    monkeypatch.setattr(
        demo_main, "run_gmail_quote", lambda approved_by: seen.append(approved_by) or 0
    )

    demo_main.main(["gmail-quote", "--env-file", str(env_file), "--approved-by", "A. Operator"])

    assert seen == ["A. Operator"]


def test_a_missing_file_stops_the_demo_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(ENV_FILE_VAR, raising=False)
    monkeypatch.setattr(
        demo_main, "run_gmail_auth", lambda: pytest.fail("must not run without its configuration")
    )

    exit_code = demo_main.main(["--env-file", str(tmp_path / "absent"), "gmail-auth"])

    assert exit_code == 2
    assert "No configuration file" in capsys.readouterr().out
    assert ENV_FILE_VAR not in os.environ


# --- web CLI --------------------------------------------------------------------


def test_the_web_entry_point_loads_the_named_file(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []

    def fake_run(*, host: str, port: int, settings: object, live: bool) -> int:
        seen.append(settings)
        return 0

    monkeypatch.setattr(web_main, "run", fake_run)

    assert web_main.main(["--env-file", str(env_file)]) == 0
    assert seen[0].gmail.test_address == "second@example.com"  # type: ignore[attr-defined]


def test_a_missing_file_stops_the_web_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(ENV_FILE_VAR, raising=False)
    monkeypatch.setattr(
        web_main, "run", lambda **_: pytest.fail("must not serve without its configuration")
    )

    exit_code = web_main.main(["--live", "--env-file", str(tmp_path / "absent")])

    assert exit_code == 2
    assert "No configuration file" in capsys.readouterr().out
