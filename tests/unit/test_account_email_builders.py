"""Account-aware Gmail source/sink builders (Phase 3a).

The builders gain an optional ``account``: given, they read that account's
address, tokens and query/sender; omitted, they behave exactly as before over
``settings.gmail.*``. These tests capture what the builders hand to the adapter
constructors — by substituting fakes for them — so nothing here needs a real
OAuth token file or a network call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from translog_quote import bootstrap
from translog_quote.config import GmailAccount, Settings
from translog_quote.config.settings import GmailSettings
from translog_quote.errors import PermanentFailure


class _FakeTransport:
    def __init__(self, *, token_path: Path, **_: Any) -> None:
        self.token_path = token_path


class _FakeSource:
    def __init__(
        self, transport: _FakeTransport, *, mailbox_address: str, query: str, **_: Any
    ) -> None:
        self.transport = transport
        self.mailbox_address = mailbox_address
        self.query = query


class _FakeSink:
    def __init__(self, transport: _FakeTransport, *, sender_address: str) -> None:
        self.transport = transport
        self.sender_address = sender_address


@pytest.fixture
def fake_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the real transports/adapters so the builders can be exercised
    without a token file or the network."""
    monkeypatch.setattr("translog_quote.adapters.email.HttpxGmailTransport", _FakeTransport)
    monkeypatch.setattr("translog_quote.adapters.email.HttpxGmailSendTransport", _FakeTransport)
    monkeypatch.setattr("translog_quote.adapters.email.GmailEmailSource", _FakeSource)
    monkeypatch.setattr("translog_quote.adapters.email.GmailEmailSink", _FakeSink)


def _single_account_settings() -> Settings:
    return Settings(
        gmail=GmailSettings(
            test_address="solo@example.com",
            token_path=Path(".secrets/read.json"),
            send_token_path=Path(".secrets/send.json"),
            sender_address=None,
            send_enabled=True,
            query="in:inbox SOLO",
        )
    )


def _account() -> GmailAccount:
    return GmailAccount(
        account_id="sales-in",
        address="sales@example.com",
        read_token_path=Path(".secrets/sales_read.json"),
        send_token_path=Path(".secrets/sales_send.json"),
        query="in:inbox SALES",
        approver_address="ops@example.com",
    )


# --- source ---------------------------------------------------------------------


def test_account_source_uses_account_address_token_and_query(fake_adapters: None) -> None:
    settings = _single_account_settings()
    account = _account()

    source = bootstrap.build_gmail_email_source(settings, account=account)

    assert source.mailbox_address == "sales@example.com"  # type: ignore[attr-defined]
    assert source.query == "in:inbox SALES"  # type: ignore[attr-defined]
    assert source.transport.token_path == Path(".secrets/sales_read.json")  # type: ignore[attr-defined]


def test_no_account_source_uses_the_single_account_settings(fake_adapters: None) -> None:
    settings = _single_account_settings()

    source = bootstrap.build_gmail_email_source(settings)

    assert source.mailbox_address == "solo@example.com"  # type: ignore[attr-defined]
    assert source.query == "in:inbox SOLO"  # type: ignore[attr-defined]
    assert source.transport.token_path == Path(".secrets/read.json")  # type: ignore[attr-defined]


def test_no_account_source_without_a_test_address_fails_with_the_original_message() -> None:
    settings = Settings(gmail=GmailSettings(test_address=None))
    with pytest.raises(PermanentFailure, match="TRANSLOG_GMAIL__TEST_ADDRESS"):
        bootstrap.build_gmail_email_source(settings)


# --- sink -----------------------------------------------------------------------


def test_account_sink_uses_account_sender_and_send_token(fake_adapters: None) -> None:
    settings = _single_account_settings()
    account = _account()

    sink = bootstrap.build_gmail_email_sink(settings, account=account)

    # effective_sender falls back to address when sender_address is unset
    assert sink.sender_address == "sales@example.com"  # type: ignore[attr-defined]
    assert sink.transport.token_path == Path(".secrets/sales_send.json")  # type: ignore[attr-defined]


def test_account_sink_honours_an_explicit_sender_override(fake_adapters: None) -> None:
    settings = _single_account_settings()
    account = _account().model_copy(update={"sender_address": "noreply@example.com"})

    sink = bootstrap.build_gmail_email_sink(settings, account=account)

    assert sink.sender_address == "noreply@example.com"  # type: ignore[attr-defined]


def test_no_account_sink_uses_the_single_account_settings(fake_adapters: None) -> None:
    settings = _single_account_settings()  # sender_address None -> falls back to test_address

    sink = bootstrap.build_gmail_email_sink(settings)

    assert sink.sender_address == "solo@example.com"  # type: ignore[attr-defined]
    assert sink.transport.token_path == Path(".secrets/send.json")  # type: ignore[attr-defined]


def test_send_enabled_is_global_and_gates_the_account_sink_too() -> None:
    settings = Settings(gmail=GmailSettings(send_enabled=False))
    with pytest.raises(PermanentFailure, match="Outbound Gmail is disabled"):
        bootstrap.build_gmail_email_sink(settings, account=_account())
