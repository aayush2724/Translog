"""Selecting a Gmail account by configuration file.

A demonstration runs against one mailbox, and a second mailbox must be
selectable without editing — or overwriting — the first one's configuration and
OAuth tokens. That is what the layered env file buys, and these tests pin the
three properties that make it safe:

- the account file wins over the base file for what it names;
- everything it does not name is inherited, so no secret is written twice;
- naming a file that is not there is an error, never a quiet fall-back to the
  base file — a demo that silently runs against the previous account is the
  exact failure this mechanism exists to prevent.

Every test runs in a temporary directory with env files it wrote itself, so it
never reads the developer's real `.env`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from translog_quote.config import ENV_FILE_VAR, load_settings

if TYPE_CHECKING:
    from pathlib import Path

BASE = """\
TRANSLOG_OPENROUTER__API_KEY=base-key-not-a-real-credential
TRANSLOG_GMAIL__TEST_ADDRESS=first@example.com
TRANSLOG_GMAIL__APPROVER_ADDRESS=first@example.com
TRANSLOG_GMAIL__TOKEN_PATH=.secrets/gmail_token.json
TRANSLOG_GMAIL__SEND_TOKEN_PATH=.secrets/gmail_send_token.json
"""

ACCOUNT = """\
TRANSLOG_GMAIL__TEST_ADDRESS=second@example.com
TRANSLOG_GMAIL__SENDER_ADDRESS=second@example.com
TRANSLOG_GMAIL__APPROVER_ADDRESS=second@example.com
TRANSLOG_GMAIL__TOKEN_PATH=.secrets/second/gmail_token.json
TRANSLOG_GMAIL__SEND_TOKEN_PATH=.secrets/second/gmail_send_token.json
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway project directory holding both env files."""
    (tmp_path / ".env").write_text(BASE, encoding="utf-8")
    (tmp_path / ".env.second").write_text(ACCOUNT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ENV_FILE_VAR, raising=False)
    # Real environment variables outrank env files in pydantic-settings, so a
    # developer's exported value would otherwise decide the assertion.
    for leaked in (
        "TRANSLOG_GMAIL__TEST_ADDRESS",
        "TRANSLOG_GMAIL__APPROVER_ADDRESS",
        "TRANSLOG_GMAIL__SENDER_ADDRESS",
        "TRANSLOG_OPENROUTER__API_KEY",
    ):
        monkeypatch.delenv(leaked, raising=False)
    return tmp_path


def test_without_an_override_only_the_base_file_is_read(project: Path) -> None:
    settings = load_settings()

    assert settings.gmail.test_address == "first@example.com"
    assert str(settings.gmail.token_path) == ".secrets/gmail_token.json"


def test_the_account_file_wins_for_what_it_names(project: Path) -> None:
    settings = load_settings(".env.second")

    assert settings.gmail.test_address == "second@example.com"
    assert settings.gmail.sender_address == "second@example.com"
    assert settings.gmail.approver_address == "second@example.com"


def test_each_account_gets_its_own_token_paths(project: Path) -> None:
    """The property that makes the switch non-destructive.

    Consenting as the second account writes to the second account's paths, so
    the first account's tokens are still where they were.
    """
    first = load_settings()
    second = load_settings(".env.second")

    assert first.gmail.token_path != second.gmail.token_path
    assert first.gmail.send_token_path != second.gmail.send_token_path
    assert str(second.gmail.token_path) == ".secrets/second/gmail_token.json"


def test_what_the_account_file_omits_is_inherited(project: Path) -> None:
    """No secret is duplicated into the account file to make it work."""
    settings = load_settings(".env.second")

    assert settings.openrouter.api_key is not None
    assert settings.openrouter.api_key.get_secret_value() == "base-key-not-a-real-credential"


def test_the_environment_variable_selects_the_same_file(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_FILE_VAR, ".env.second")

    assert load_settings().gmail.test_address == "second@example.com"


def test_an_explicit_argument_beats_the_environment_variable(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_FILE_VAR, ".env.second")

    assert load_settings(".env").gmail.test_address == "first@example.com"


def test_a_missing_account_file_is_refused_not_ignored(project: Path) -> None:
    """Falling back would run the demo against the other account silently."""
    with pytest.raises(FileNotFoundError) as raised:
        load_settings(".env.absent")

    assert ".env.absent" in str(raised.value)


def test_a_missing_file_named_by_the_environment_variable_is_refused(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_FILE_VAR, ".env.absent")

    with pytest.raises(FileNotFoundError):
        load_settings()


def test_an_empty_environment_variable_means_no_override(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset-but-exported variable must not be read as a filename."""
    monkeypatch.setenv(ENV_FILE_VAR, "")

    assert load_settings().gmail.test_address == "first@example.com"


# --- the refusal that has to point at the right grant ----------------------------


def test_a_missing_send_token_names_the_send_consent_command(tmp_path: Path) -> None:
    """`gmail-auth` grants read, not send.

    Naming it here would send the operator to a grant that leaves the sink just
    as broken — and, run without `--env-file`, would overwrite the other
    account's read token on the way.
    """
    from translog_quote.adapters.email.gmail_send import HttpxGmailSendTransport
    from translog_quote.errors import PermanentFailure

    with pytest.raises(PermanentFailure) as raised:
        HttpxGmailSendTransport(
            token_path=tmp_path / "absent.json", timeout_seconds=5, max_retries=0
        )

    assert "gmail-auth-send" in str(raised.value)


@pytest.mark.parametrize("command", ["gmail-auth", "gmail-auth-send"])
def test_a_missing_token_message_warns_about_the_env_file(tmp_path: Path, command: str) -> None:
    """Following the instruction verbatim must not overwrite another account.

    Consent writes to whichever token path the *loaded* configuration names, so
    the flag that selects the configuration belongs in the instruction itself.
    """
    from translog_quote.adapters.email.gmail import _load_token_file
    from translog_quote.errors import PermanentFailure

    with pytest.raises(PermanentFailure) as raised:
        _load_token_file(tmp_path / "absent.json", command=command)

    message = str(raised.value)
    assert "--env-file" in message
    assert command in message
