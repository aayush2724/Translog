"""The read-only health check's verdict logic. No network, Redis, or subprocess:
the pure helpers and the check functions are driven with fakes.
"""

from __future__ import annotations

from translog_quote.interface.jobs.queue import WORKER_STATUS_KEY
from translog_quote.ops import healthcheck as hc


def test_render_lists_findings_and_summarises() -> None:
    findings = [hc.Finding("2 /health", hc.PASS, "ok"), hc.Finding("3 redis", hc.FAIL, "down")]
    table = hc.render(findings)
    assert "2 /health" in table and "3 redis" in table
    assert "FAIL" in table and "1 FAIL" in table


def test_exit_code_is_nonzero_only_on_a_fail() -> None:
    assert hc.exit_code([hc.Finding("x", hc.PASS, ""), hc.Finding("y", hc.WARN, "")]) == 0
    assert hc.exit_code([hc.Finding("x", hc.NEEDS_APPROVAL, "")]) == 0
    assert hc.exit_code([hc.Finding("x", hc.FAIL, "")]) == 1


class _RedisInfoConn:
    def __init__(self, info: dict[str, object]) -> None:
        self._info = info

    def ping(self) -> bool:
        return True

    def info(self) -> dict[str, object]:
        return self._info


def test_eviction_policy_that_can_drop_our_keys_is_a_fail() -> None:
    findings = hc.check_redis(
        _RedisInfoConn(
            {"maxmemory_policy": "allkeys-lru", "redis_version": "8", "connected_clients": 2}
        )
    )
    assert any(f.check == "3 eviction policy" and f.verdict == hc.FAIL for f in findings)


def test_noeviction_policy_passes() -> None:
    findings = hc.check_redis(
        _RedisInfoConn(
            {"maxmemory_policy": "noeviction", "redis_version": "8", "connected_clients": 2}
        )
    )
    assert any(f.check == "3 eviction policy" and f.verdict == hc.PASS for f in findings)


class _KeysConn:
    def __init__(self, mapping: dict[str, object]) -> None:
        self._m = mapping

    def get(self, key: str) -> object:
        return self._m.get(key)

    def ttl(self, _key: str) -> int:
        return 86


def test_needs_login_while_the_lock_is_held_is_flagged() -> None:
    conn = _KeysConn({"lockkey": b"tok", WORKER_STATUS_KEY: b"needs_login"})
    findings = hc.check_keys(conn, "lockkey")
    assert any(f.check == "5 consistency" and f.verdict == hc.FAIL for f in findings)


def test_a_held_lock_with_no_status_is_consistent() -> None:
    findings = hc.check_keys(_KeysConn({"lockkey": b"tok"}), "lockkey")
    assert not any(f.check == "5 consistency" for f in findings)
    assert any(f.check == "5 lock" and f.verdict == hc.PASS for f in findings)


_SNAPSHOT = {
    "requests": [
        {"request_id": "R1", "status": {"state": "validated"}},  # stuck: nothing pending
        {"request_id": "R2", "status": {"state": "validated"}, "goods_type_hold": {"catalog": []}},
        {"request_id": "R3", "status": {"state": "needs_info"}, "awaiting_clarification": True},
        {"request_id": "R4", "status": {"state": "validated"}, "awaiting_decision": True},
        {"request_id": "R5", "status": {"state": "no_eligible_rate"}},  # terminal -> not stuck
        {"request_id": "R6", "status": {"state": "validated"}, "rate_search_pending": True},
    ],
    "poll": {"last_checked_at": "2026-09-16T12:00:00+00:00", "error": None},
}


def test_dashboard_findings_flag_only_the_stuck_request() -> None:
    by = {f.check: f for f in hc._dashboard_findings(_SNAPSHOT)}
    stuck = by["8 stuck (nothing pending)"]
    assert stuck.verdict == hc.WARN
    assert "R1" in stuck.detail
    # a hold, a clarification, an approval, a terminal, and an in-flight job are NOT stuck
    for other in ("R2", "R3", "R4", "R5", "R6"):
        assert other not in stuck.detail
    assert by["9 mailbox poll"].verdict == hc.PASS


def test_a_stranded_extracted_request_is_flagged_as_stuck() -> None:
    """Point 4 safety net. Extraction statuses are not persisted, so a request
    stranded at EXTRACTED by an old build (before the denied-MSDS fix / the
    EXTRACTED->MANUAL_REVIEW edge) cannot be re-classified on restore. It has
    nothing pending, so the healthcheck surfaces it for a human to look at."""
    snapshot = {
        "requests": [{"request_id": "R-old-stuck", "status": {"state": "extracted"}}],
        "poll": {"last_checked_at": "2026-09-16T12:00:00+00:00", "error": None},
    }

    by = {f.check: f for f in hc._dashboard_findings(snapshot)}

    stuck = by["8 stuck (nothing pending)"]
    assert stuck.verdict == hc.WARN
    assert "R-old-stuck" in stuck.detail


def test_dashboard_findings_fail_on_a_poll_error() -> None:
    snapshot = {"requests": [], "poll": {"last_checked_at": "x", "error": "TimeoutError"}}
    by = {f.check: f for f in hc._dashboard_findings(snapshot)}
    assert by["9 mailbox poll"].verdict == hc.FAIL
    assert by["8 stuck (nothing pending)"].verdict == hc.PASS  # none stuck


def test_zero_active_reports_the_cutoff_not_a_bare_pass() -> None:
    """Req 5. An empty active list after a deploy is exactly the symptom this
    change addresses, so it is reported with the cutoff, not a silent PASS."""
    snapshot = {
        "requests": [],
        "demonstration": {"startup_mode": "operations", "started_at": "2026-09-16T00:00:00+00:00"},
        "poll": {"last_checked_at": "x", "error": None},
    }
    by = {f.check: f for f in hc._dashboard_findings(snapshot)}
    detail = by["8 states (active)"].detail
    assert "0 active requests (demonstration started at 2026-09-16" in detail


def test_operations_flags_a_stale_operator_owned_request() -> None:
    """Req 5. In operations mode a VALIDATED request older than the operator
    threshold is flagged; a CLARIFICATION_SENT of the same age is not, because it
    waits on the client and gets the longer window."""
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    seen = (now - timedelta(hours=30)).isoformat()
    snapshot = {
        "requests": [
            {"request_id": "R-old", "status": {"state": "validated"}, "first_seen_at": seen},
            {
                "request_id": "R-clar",
                "status": {"state": "clarification_sent"},
                "first_seen_at": seen,
            },
        ],
        "demonstration": {"startup_mode": "operations"},
        "poll": {"last_checked_at": "x", "error": None},
    }
    by = {
        f.check: f
        for f in hc._dashboard_findings(
            snapshot, now=now, stale_operator_hours=24.0, stale_clarification_hours=72.0
        )
    }
    stale = by["8 stale (operations)"]
    assert stale.verdict == hc.WARN
    assert "R-old" in stale.detail
    assert "R-clar" not in stale.detail


def test_demonstration_mode_runs_no_stale_check() -> None:
    """The stale check is operations-only: a fresh demonstration legitimately
    empties the view, so an age warning there would be noise."""
    snapshot = {
        "requests": [{"request_id": "R", "status": {"state": "validated"}, "first_seen_at": "x"}],
        "demonstration": {"startup_mode": "demonstration"},
        "poll": {"last_checked_at": "x", "error": None},
    }
    checks = {f.check for f in hc._dashboard_findings(snapshot)}
    assert "8 stale (operations)" not in checks
