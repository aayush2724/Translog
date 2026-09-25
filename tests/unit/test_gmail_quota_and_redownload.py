"""Gmail quota: no re-downloading handled mail, and a per-account quota hold.

Production hit "Quota exceeded for quota metric 'Total Query Cost' … 'Units per
minute per user'" (HTTP 403). Two causes, both in how a poll spends quota:

1. Every poll fully downloaded every message in the ``after:`` window, because
   the "already handled" check keys on the Message-ID, which is only known after
   the download. With a watermark held open by an unsent draft, that is a whole
   day and more of inbox, every 10 seconds. The source now remembers each Gmail
   id's Message-ID in memory and skips the download only when ``seen`` confirms
   that Message-ID *now* — a pure optimisation: a miss, or an unconfirmed hit,
   takes exactly the old full-download path.
2. A quota 403 was retried at the next poll, 10 seconds later, keeping the
   mailbox over its per-minute limit. It is now recognised as a quota refusal
   (with wording that says so), and that mailbox's reads pause for 60 seconds —
   per account, never an extraction failure.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.unit.test_gmail_email_source import MAILBOX, PagingTransport, dated
from tests.unit.test_gmail_thread import StubSource
from tests.unit.test_gmail_transport import SLEPT, scripted, transport_with

from translog_quote import bootstrap
from translog_quote.adapters.email import CollectingEmailSink, GmailEmailSource
from translog_quote.adapters.email.gmail import (
    QUOTA_HOLD_MAX_SECONDS,
    QUOTA_HOLD_SECONDS,
    GmailQuotaExceeded,
)
from translog_quote.config import Settings
from translog_quote.config.settings import DemoSettings, GmailSettings, OpenRouterSettings
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.workflow import RequestState
from translog_quote.errors import PermanentFailure
from translog_quote.pipeline.audit import AuditEventType

SINCE = datetime(2026, 8, 20, tzinfo=UTC)


def _bodies(*ids: str) -> dict[str, Any]:
    return {
        f"messages/{i}": dated(i, f"Mon, 24 Aug 2026 09:0{n}:00 +0000", f"<{i}@x>")
        for n, i in enumerate(ids)
    }


def _downloads(transport: PagingTransport) -> list[str]:
    return [path.removeprefix("messages/") for path, _ in transport.calls if "/" in path]


class Handled:
    """The session's `seen`: a set a test can grow or shrink between polls."""

    def __init__(self) -> None:
        self.ids: set[str] = set()
        self.asked: list[str] = []

    def __call__(self, message_id: str) -> bool:
        self.asked.append(message_id)
        return message_id in self.ids


def _source(transport: PagingTransport, seen: Handled) -> GmailEmailSource:
    return GmailEmailSource(
        transport, mailbox_address=MAILBOX, max_results=25, overlap_seconds=0.0, seen=seen
    )


# --- 1. already-handled mail is not downloaded again --------------------------------------


def test_a_handled_message_is_not_downloaded_again_on_the_next_poll() -> None:
    transport = PagingTransport([(["m2", "m1"], None)], _bodies("m1", "m2"))
    seen = Handled()
    source = _source(transport, seen)

    first = source.fetch_new(since=SINCE)
    assert [e.message_id for e in first] == ["<m1@x>", "<m2@x>"]
    assert _downloads(transport) == ["m1", "m2"]

    seen.ids.add("<m1@x>")  # the session handled m1
    transport.calls.clear()
    second = source.fetch_new(since=SINCE)

    assert _downloads(transport) == ["m2"], "m1 skipped without a download"
    assert [e.message_id for e in second] == ["<m2@x>"]
    assert "<m1@x>" in seen.asked, "the skip was confirmed by seen, not assumed"


def test_an_unhandled_message_is_still_downloaded_every_poll() -> None:
    """A held, deferred or not-yet-routed message is not "seen": it takes the
    full path each poll, so it can still be processed."""
    transport = PagingTransport([(["m1"], None)], _bodies("m1"))
    source = _source(transport, Handled())

    for _ in range(3):
        assert [e.message_id for e in source.fetch_new(since=SINCE)] == ["<m1@x>"]

    assert _downloads(transport) == ["m1", "m1", "m1"]


def test_a_cached_id_that_seen_no_longer_confirms_is_downloaded_again() -> None:
    """The memo is never the source of truth: `seen` is asked every time."""
    transport = PagingTransport([(["m1"], None)], _bodies("m1"))
    seen = Handled()
    source = _source(transport, seen)
    source.fetch_new(since=SINCE)
    seen.ids.add("<m1@x>")
    source.fetch_new(since=SINCE)
    seen.ids.clear()
    transport.calls.clear()

    assert [e.message_id for e in source.fetch_new(since=SINCE)] == ["<m1@x>"]
    assert _downloads(transport) == ["m1"]


def test_a_new_reply_is_always_downloaded() -> None:
    bodies = _bodies("m1", "r1")
    pages = [(["m1"], None)]
    transport = PagingTransport(pages, bodies)
    seen = Handled()
    source = _source(transport, seen)
    source.fetch_new(since=SINCE)
    seen.ids.add("<m1@x>")

    pages[0] = (["r1", "m1"], None)  # the client's reply arrives
    transport.calls.clear()

    assert [e.message_id for e in source.fetch_new(since=SINCE)] == ["<r1@x>"]
    assert _downloads(transport) == ["r1"]


def test_a_restart_downloads_once_then_skips() -> None:
    """A fresh source has an empty memo: the first poll behaves exactly as
    before (download, then skip on seen), later polls skip without it."""
    transport = PagingTransport([(["m1"], None)], _bodies("m1"))
    seen = Handled()
    seen.ids.add("<m1@x>")  # durably committed before the restart
    source = _source(transport, seen)

    assert source.fetch_new(since=SINCE) == ()
    assert source.fetch_new(since=SINCE) == ()

    assert _downloads(transport) == ["m1"], "downloaded once after the restart, never again"


def test_two_mailboxes_never_share_the_memo() -> None:
    seen = Handled()
    seen.ids.add("<m1@x>")
    first = PagingTransport([(["m1"], None)], _bodies("m1"))
    second = PagingTransport([(["m1"], None)], _bodies("m1"))
    _source(first, seen).fetch_new(since=SINCE)

    _source(second, seen).fetch_new(since=SINCE)

    assert _downloads(second) == ["m1"], "the other account downloads for itself"


def test_the_memo_forgets_ids_that_left_the_window() -> None:
    pages = [(["m2", "m1"], None)]
    transport = PagingTransport(pages, _bodies("m1", "m2"))
    source = _source(transport, Handled())
    source.fetch_new(since=SINCE)
    assert set(source._message_id_by_gmail_id) == {"m1", "m2"}

    pages[0] = (["m2"], None)  # m1 fell out of the after: window
    source.fetch_new(since=SINCE)

    assert set(source._message_id_by_gmail_id) == {"m2"}


def test_the_demonstration_path_is_unchanged() -> None:
    """`since=None` never used the seen skip, and does not use the memo."""
    responses = {
        "profile": {"emailAddress": MAILBOX},
        "messages": {"messages": [{"id": "m1"}]},
        **_bodies("m1"),
    }
    calls: list[str] = []

    class Plain:
        def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
            calls.append(path)
            return responses[path]  # type: ignore[no-any-return]

    seen = Handled()
    seen.ids.add("<m1@x>")
    source = GmailEmailSource(Plain(), mailbox_address=MAILBOX, max_results=1, seen=seen)
    source.fetch_new()
    source.fetch_new()

    assert calls.count("messages/m1") == 2


# --- 2. a quota 403 is named for what it is, and never retried in the poll ---------------

QUOTA_BODY = {
    "error": {
        "code": 403,
        "message": "Quota exceeded for quota metric 'Total Query Cost' and limit "
        "'Units per minute per user' of service 'gmail.googleapis.com'",
    }
}


@pytest.mark.parametrize(
    "body",
    [
        QUOTA_BODY,
        {"error": {"message": "Rate Limit Exceeded", "errors": [{"reason": "rateLimitExceeded"}]}},
        {"error": {"message": "slow down", "errors": [{"reason": "userRateLimitExceeded"}]}},
        {"error": {"message": "slow down", "status": "RESOURCE_EXHAUSTED"}},
    ],
    ids=["production-message", "rateLimitExceeded", "userRateLimitExceeded", "RESOURCE_EXHAUSTED"],
)
def test_a_quota_403_says_quota_not_consent_and_is_not_retried(
    tmp_path: Path, body: dict[str, Any]
) -> None:
    handler, seen = scripted([httpx.Response(403, json=body)])

    with pytest.raises(GmailQuotaExceeded) as caught:
        transport_with(tmp_path, handler, max_retries=2).get_json("profile")

    assert "quota/rate limit exceeded" in str(caught.value)
    assert "gmail.readonly" not in str(caught.value)
    assert SLEPT == [], "not retried inside the poll"
    assert len([r for r in seen if r.method == "GET"]) == 1


def test_a_quota_403_carries_gmails_retry_after(tmp_path: Path) -> None:
    handler, _ = scripted([httpx.Response(403, json=QUOTA_BODY, headers={"Retry-After": "120"})])

    with pytest.raises(GmailQuotaExceeded) as caught:
        transport_with(tmp_path, handler).get_json("profile")

    assert caught.value.retry_after == 120.0


def test_a_permission_403_keeps_the_consent_wording(tmp_path: Path) -> None:
    handler, _ = scripted(
        [httpx.Response(403, json={"error": {"message": "Request had insufficient scopes"}})]
    )

    with pytest.raises(PermanentFailure, match="gmail.readonly") as caught:
        transport_with(tmp_path, handler).get_json("profile")

    assert not isinstance(caught.value, GmailQuotaExceeded)


# --- 3. the per-account hold -------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class QuotaTransport:
    """Refuses with a quota 403 until `refuse` is cleared; counts every call."""

    def __init__(self, retry_after: float | None = None) -> None:
        self.refuse = True
        self.retry_after = retry_after
        self.calls = 0

    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        self.calls += 1
        if self.refuse:
            raise GmailQuotaExceeded(
                "Gmail API quota/rate limit exceeded (403): …", retry_after=self.retry_after
            )
        if path == "profile":
            return {"emailAddress": MAILBOX}
        return {}  # an empty listing


def _held_source(transport: QuotaTransport, clock: Clock) -> GmailEmailSource:
    return GmailEmailSource(
        transport, mailbox_address=MAILBOX, max_results=25, seen=Handled(), monotonic=clock
    )


def test_after_a_quota_refusal_the_mailbox_is_not_contacted_for_60_seconds() -> None:
    clock = Clock()
    transport = QuotaTransport()
    source = _held_source(transport, clock)

    with pytest.raises(GmailQuotaExceeded):
        source.fetch_new(since=SINCE)
    assert transport.calls == 1

    for _ in range(5):  # a poll every 10 seconds, all inside the hold
        clock.now += 10
        with pytest.raises(GmailQuotaExceeded, match="paused"):
            source.fetch_new(since=SINCE)
    assert transport.calls == 1, "no Gmail call while held"

    transport.refuse = False
    clock.now = 1000.0 + QUOTA_HOLD_SECONDS + 1
    assert source.fetch_new(since=SINCE) == ()
    assert transport.calls > 1, "reads resume after the hold"


def test_a_longer_retry_after_is_honoured_and_capped() -> None:
    clock = Clock()
    source = _held_source(QuotaTransport(retry_after=300), clock)
    with pytest.raises(GmailQuotaExceeded):
        source.fetch_new(since=SINCE)
    assert source._quota_hold_until == 1000.0 + 300

    capped = _held_source(QuotaTransport(retry_after=10_000), clock)
    with pytest.raises(GmailQuotaExceeded):
        capped.fetch_new(since=SINCE)
    assert capped._quota_hold_until == 1000.0 + QUOTA_HOLD_MAX_SECONDS


def test_one_accounts_quota_hold_does_not_stop_another_and_is_not_an_extraction_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_multi_account_session import _write_accounts
    from tests.unit.test_reply_gate_and_extraction_isolation import ProviderExtractor

    from translog_quote.interface.web.multi_account_session import MultiAccountSession

    clock = Clock()
    quota = QuotaTransport()
    held_source = _held_source(quota, clock)
    enquiry_b = RawEmail(
        message_id="<q-b@c.example>",
        from_address="client@example.com",
        subject="Rate required - B",
        body_text="enquiry for B",
        received_at=datetime(2026, 9, 22, 9, 5, tzinfo=UTC),
    )
    extractor = ProviderExtractor(
        {
            "enquiry for B": ExtractionResult(
                origin=ExtractedValue[str].stated("Delhi (DEL)"),
                ship_date=ExtractedValue[date].stated(date(2026, 12, 1)),
            )
        }
    )
    sources = {"acct-a": held_source, "acct-b": StubSource(enquiry_b)}
    monkeypatch.setattr(bootstrap, "build_extractor", lambda settings: extractor)
    monkeypatch.setattr(
        bootstrap, "build_gmail_email_sink", lambda settings, *, account=None: CollectingEmailSink()
    )
    monkeypatch.setattr(
        bootstrap,
        "build_gmail_email_source",
        lambda settings, *, account=None, **_: sources[account.account_id],
    )
    _write_accounts(tmp_path / "config", ("acct-a", True), ("acct-b", True))
    ms = MultiAccountSession.build(
        Settings(
            openrouter=OpenRouterSettings(api_key="k"),  # type: ignore[arg-type]
            gmail=GmailSettings(
                accounts_dir=tmp_path / "config",
                test_address="solo@example.com",
                approver_address="ops@example.com",
                send_enabled=True,
            ),
            demo=DemoSettings(
                state_dir=tmp_path / "state",
                startup_mode="operations",
                operations_since=datetime(2026, 9, 20, tzinfo=UTC),
            ),
        )
    )
    watermark_before = ms.sessions["acct-a"].demonstration.last_poll_watermark

    ms.poll()
    clock.now += 10
    ms.poll()

    a = ms.sessions["acct-a"]
    assert a.last_poll_error == "GmailQuotaExceeded"
    assert quota.calls == 1, "account A was contacted once, then held"
    assert a.demonstration.last_poll_watermark == watermark_before, "watermark untouched"
    assert AuditEventType.EXTRACTION_UNAVAILABLE not in {e.event for e in a.audit.events}
    assert a._extraction_hold == {}
    [b_request] = ms.sessions["acct-b"].requests.values()
    assert b_request.state is RequestState.NEEDS_INFO, "account B carried on"
    assert ms.sessions["acct-b"].last_poll_error is None
    ms.close()
