"""End-to-end: operations mode over the Redis backend survives a restart.

A shared FakeRedis stands in for Upstash; ``bootstrap.build_redis_client`` is
monkeypatched to return it, so a LiveSession built with
``durable_backend="redis"`` stores requests, threads, the watermark and audit in
that fake. Handing the same fake to a second LiveSession models a Render restart
with no persistent disk — the failure the whole change exists to fix.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.fake_redis import FakeRedis
from tests.unit.test_gmail_thread import ScriptedExtractor, StubSource
from tests.unit.test_manual_review_escalation import (
    ENQUIRY,
    MALFORMED,
    PoisonExtractor,
    enquiry_extraction,
)

from translog_quote.adapters.clock import FixedClock
from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.routing import StatedLocationResolver
from translog_quote.adapters.store import JsonFileStore
from translog_quote.adapters.store.redis import RedisStore
from translog_quote.config import Settings
from translog_quote.domain.workflow import RequestState
from translog_quote.interface.web.audit_log import JsonFileAuditLog
from translog_quote.interface.web.live_session import LiveSession
from translog_quote.interface.web.redis_state import RedisAuditLog, RedisDemonstrationStore


def _base_settings(*, backend: str) -> Settings:
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    return base.model_copy(
        update={
            "openrouter": base.openrouter.model_copy(update={"api_key": "test-not-a-credential"}),
            "demo": base.demo.model_copy(
                update={
                    "state_dir": Path(tempfile.mkdtemp()),
                    "durable_backend": backend,
                    "startup_mode": "operations",
                    "operations_since": datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
                }
            ),
            "gmail": base.gmail.model_copy(
                update={
                    "test_address": "translog@example.com",
                    "sender_address": "translog@example.com",
                    "approver_address": "approvals@translog.example",
                    "send_enabled": True,
                }
            ),
        }
    )


def _session(settings: Settings, source: StubSource, sink: CollectingEmailSink, extractor: object):  # type: ignore[no-untyped-def]
    session = LiveSession(
        settings,
        source=source,  # type: ignore[arg-type]
        sink=sink,
        extractor=extractor,  # type: ignore[arg-type]
        resolver=StatedLocationResolver(),
        clock=FixedClock(datetime(2026, 9, 1, 10, 0, tzinfo=UTC)),
    )
    session.resume_operations()  # what create_session() does in operations mode
    return session


def test_redis_backend_is_selected_for_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    monkeypatch.setattr("translog_quote.bootstrap.build_redis_client", lambda _s: fake)
    session = _session(
        _base_settings(backend="redis"), StubSource(), CollectingEmailSink(), ScriptedExtractor()
    )

    assert isinstance(session._durable, RedisStore)
    assert isinstance(session.audit, RedisAuditLog)
    assert isinstance(session._demonstration, RedisDemonstrationStore)


def test_filesystem_backend_stays_the_default() -> None:
    session = _session(
        _base_settings(backend="filesystem"),
        StubSource(),
        CollectingEmailSink(),
        ScriptedExtractor(),
    )
    assert isinstance(session._durable, JsonFileStore)
    assert isinstance(session.audit, JsonFileAuditLog)


def test_operations_watermark_persists_across_restart_on_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRedis()
    monkeypatch.setattr("translog_quote.bootstrap.build_redis_client", lambda _s: fake)

    # First process: seed the watermark from operations_since (no watermark yet).
    first = _session(
        _base_settings(backend="redis"), StubSource(), CollectingEmailSink(), ScriptedExtractor()
    )
    seeded = first._demonstration.current.last_poll_watermark
    assert seeded == datetime(2026, 9, 1, 0, 0, tzinfo=UTC)

    # Restart: a fresh session over the same Redis reloads the persisted watermark
    # rather than re-seeding — it is durable, not a process-local timer.
    reborn = _session(
        _base_settings(backend="redis"), StubSource(), CollectingEmailSink(), ScriptedExtractor()
    )
    assert reborn._demonstration.current.last_poll_watermark == seeded


def test_a_malformed_email_is_deduped_and_not_renotified_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Edge-Test-7 regression, now across a Redis restart: the malformed
    message is handed to MANUAL_REVIEW with one client failure notice, and a
    restart does not re-extract it or re-send the notice."""
    fake = FakeRedis()
    monkeypatch.setattr("translog_quote.bootstrap.build_redis_client", lambda _s: fake)
    settings = _base_settings(backend="redis")

    sink1 = CollectingEmailSink()
    first = _session(settings, StubSource(MALFORMED), sink1, PoisonExtractor(MALFORMED.body_text))
    first.poll()
    assert any(r.state is RequestState.MANUAL_REVIEW for r in first.requests.values())
    notices_1 = [m for m in sink1.sent if m.to_address == MALFORMED.from_address]
    assert len(notices_1) == 1

    # Restart: same Redis, fresh session/extractor/sink.
    sink2 = CollectingEmailSink()
    reborn_extractor = PoisonExtractor(MALFORMED.body_text)
    reborn = _session(settings, StubSource(MALFORMED), sink2, reborn_extractor)
    reborn.poll()

    assert reborn_extractor.calls.count(MALFORMED.body_text) == 0, "already processed, not re-run"
    assert [m for m in sink2.sent if m.to_address == MALFORMED.from_address] == [], (
        "no duplicate failure notice after restart"
    )
    # The MANUAL_REVIEW request is restored from Redis.
    assert [r for r in reborn._durable.all_requests() if r.state is RequestState.MANUAL_REVIEW]


def test_watermark_holds_at_an_uncommitted_needs_info_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NEEDS_INFO draft is never durably committed, so the watermark must not
    advance past it — it holds at the message and a restart re-derives the draft."""
    fake = FakeRedis()
    monkeypatch.setattr("translog_quote.bootstrap.build_redis_client", lambda _s: fake)
    settings = _base_settings(backend="redis")

    session = _session(
        settings,
        StubSource(ENQUIRY),
        CollectingEmailSink(),
        ScriptedExtractor(enquiry_extraction()),
    )
    session.poll()

    assert any(r.state is RequestState.NEEDS_INFO for r in session.requests.values())
    assert session._durable.all_threads() == (), "a NEEDS_INFO draft commits nothing durably"
    # Held at the uncommitted message, not advanced past it.
    assert session._demonstration.current.last_poll_watermark == ENQUIRY.received_at

    # Restart: the draft was never committed, so it is re-fetched and re-derived.
    reborn_extractor = ScriptedExtractor(enquiry_extraction())
    reborn = _session(settings, StubSource(ENQUIRY), CollectingEmailSink(), reborn_extractor)
    reborn.poll()
    assert reborn_extractor.calls, "the uncommitted enquiry was re-extracted after restart"
