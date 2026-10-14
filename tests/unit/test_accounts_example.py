"""The committed 16-slot template directory stays loadable.

``deploy/accounts.example/`` is documentation that executes: every slot a..p is
a file the real loader must accept, ``account-a``/``account-b`` stand in for the
two live mailboxes, and ``c``..``p`` ship *disabled* so a wholesale copy of the
directory never polls, or replies from, a placeholder address. Each slot owns
its own token paths, so the loader's shared-credential refusal has nothing to
trip on once the slots are enabled one by one.
"""

from __future__ import annotations

from pathlib import Path

from translog_quote.config.settings import (
    GmailAccount,
    GmailSettings,
    Settings,
    resolve_gmail_accounts,
)

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "deploy" / "accounts.example"
SLOTS = "abcdefghijklmnop"
PRODUCTION_QUERY = 'in:inbox -subject:"[TRANSLOG INTERNAL]"'


def _load() -> tuple[GmailAccount, ...]:
    return resolve_gmail_accounts(Settings(gmail=GmailSettings(accounts_dir=EXAMPLE_DIR)))


def test_sixteen_slots_load_in_order_with_their_own_tokens() -> None:
    accounts = _load()
    assert [account.account_id for account in accounts] == [f"account-{s}" for s in SLOTS]
    assert [account.read_token_path for account in accounts] == [
        Path(f".secrets/accounts/{s}_read.json") for s in SLOTS
    ]
    assert [account.send_token_path for account in accounts] == [
        Path(f".secrets/accounts/{s}_send.json") for s in SLOTS
    ]


def test_only_the_two_live_examples_are_enabled() -> None:
    enabled = [account.account_id for account in _load() if account.enabled]
    assert enabled == ["account-a", "account-b"]


def test_every_slot_carries_the_production_query_and_a_placeholder_address() -> None:
    for account in _load():
        assert account.query == PRODUCTION_QUERY
        assert account.address.endswith("@example.com"), account.account_id
