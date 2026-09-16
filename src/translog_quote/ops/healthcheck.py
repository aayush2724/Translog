"""Read-only health check for the live pipeline. Re-run after every deploy:

    python -m translog_quote.ops.healthcheck [--url https://<dashboard>] [--env-file .env.worker]

Performs checks 2-10 of the manual audit and prints a PASS/WARN/FAIL table.
STRICTLY read-only: HTTP GETs, Redis PING/INFO/GET/TTL/ZCARD/ZRANGE/LLEN, and
`systemctl`/`journalctl`/`timedatectl` reads. No SCAN, no registry cleanup, no
writes, no live actions. Anything that would need a write or a credential we do
not have prints NEEDS_APPROVAL rather than acting. Exit code is non-zero if any
check FAILs, so a deploy can be gated on it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from translog_quote import bootstrap
from translog_quote.interface.jobs import build_redis
from translog_quote.interface.jobs.queue import WORKER_REQUEUE_KEY, WORKER_STATUS_KEY

PASS, WARN, FAIL, SKIP, NEEDS_APPROVAL = "PASS", "WARN", "FAIL", "SKIP", "NEEDS_APPROVAL"

_DEFAULT_URL = "https://translog-demo.onrender.com"
_UNIT = "translog-webcargo-worker"


@dataclass(frozen=True)
class Finding:
    check: str
    verdict: str
    detail: str


def _text(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _get_json(url: str, *, timeout: float) -> tuple[int, dict[str, object]]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed https health URL
        return resp.status, json.loads(resp.read() or b"{}")


def check_render(url: str) -> list[Finding]:
    out: list[Finding] = []
    try:
        status, body = _get_json(f"{url}/health", timeout=10)
        ok = status == 200 and body.get("status") == "ok"
        out.append(Finding("2 /health", PASS if ok else FAIL, f"{status} {body}"))
    except Exception as exc:  # noqa: BLE001
        out.append(Finding("2 /health", FAIL, f"unreachable: {exc}"))
    try:
        t0 = time.perf_counter()
        status, body = _get_json(f"{url}/health/ready", timeout=40)
        ms = (time.perf_counter() - t0) * 1000
        ok = body.get("mode") == "browser" and body.get("redis") == "ok"
        out.append(
            Finding("2 /health/ready", PASS if ok else FAIL, f"{status} {body} ({ms:.0f}ms)")
        )
    except Exception as exc:  # noqa: BLE001
        out.append(Finding("2 /health/ready", FAIL, f"unreachable: {exc}"))
    return out


def check_redis(conn: Any) -> list[Finding]:
    lat: list[float] = []
    for _ in range(5):
        t0 = time.perf_counter()
        conn.ping()
        lat.append((time.perf_counter() - t0) * 1000)
    info = conn.info()
    policy = info.get("maxmemory_policy")
    avg = sum(lat) / len(lat)
    findings = [
        Finding(
            "3 redis ping",
            WARN if avg > 150 else PASS,
            f"min/avg/max {min(lat):.0f}/{avg:.0f}/{max(lat):.0f}ms, "
            f"v{info.get('redis_version')}, clients={info.get('connected_clients')}",
        )
    ]
    # Only 'noeviction' and 'volatile-*' guarantee our (TTL-less) keys survive;
    # any 'allkeys-*' policy can silently drop them.
    evicts = isinstance(policy, str) and policy.startswith("allkeys")
    findings.append(
        Finding("3 eviction policy", FAIL if evicts else PASS, f"maxmemory_policy={policy!r}")
    )
    return findings


def check_queue(conn: Any, queue_name: str, job_timeout: int) -> list[Finding]:
    from rq.job import Job
    from rq.registry import (
        DeferredJobRegistry,
        FailedJobRegistry,
        FinishedJobRegistry,
        ScheduledJobRegistry,
        StartedJobRegistry,
    )

    counts: dict[str, int] = {"queued": conn.llen(f"rq:queue:{queue_name}")}
    regs = {
        "started": StartedJobRegistry(queue_name, connection=conn),
        "finished": FinishedJobRegistry(queue_name, connection=conn),
        "failed": FailedJobRegistry(queue_name, connection=conn),
        "deferred": DeferredJobRegistry(queue_name, connection=conn),
        "scheduled": ScheduledJobRegistry(queue_name, connection=conn),  # type: ignore[no-untyped-call]
    }
    for name, reg in regs.items():
        counts[name] = conn.zcard(reg.key)
    summary = ", ".join(f"{k}={v}" for k, v in counts.items())
    findings = [Finding("4 queue counts", PASS, summary)]

    workers = {_text(x) for x in conn.smembers("rq:workers")}
    stale_started = 0
    for jid in [_text(x) for x in conn.zrange(regs["started"].key, 0, -1)]:
        try:
            job = Job.fetch(jid, connection=conn)
            age = time.time() - job.started_at.timestamp() if job.started_at else 0
            alive = f"rq:worker:{job.worker_name}" in workers or job.worker_name in workers
            if age > job_timeout or not alive:
                stale_started += 1
        except Exception:  # noqa: BLE001
            stale_started += 1
    if counts["started"]:
        findings.append(
            Finding(
                "4 started jobs",
                WARN if stale_started else PASS,
                f"{counts['started']} started, {stale_started} stale/orphaned",
            )
        )
    if counts["failed"]:
        findings.append(
            Finding(
                "4 failed jobs",
                WARN,
                f"{counts['failed']} in FailedJobRegistry (stale unless a live request points "
                "to one; confirm via the dashboard — check 8)",
            )
        )
    return findings


def check_keys(conn: Any, lock_key: str) -> list[Finding]:
    lock = conn.get(lock_key)
    lock_ttl = conn.ttl(lock_key)
    status = conn.get(WORKER_STATUS_KEY)
    requeue = conn.get(WORKER_REQUEUE_KEY)
    findings = [
        Finding(
            "5 lock",
            PASS if lock is not None else WARN,
            f"held (ttl={lock_ttl})" if lock is not None else "not held (no worker refreshing)",
        ),
        Finding("5 worker-status", PASS, _text(status) if status is not None else "absent"),
        Finding("5 requeue-pending", PASS, "absent" if requeue is None else _text(requeue)),
    ]
    if lock is not None and status is not None and _text(status) == "needs_login":
        findings.append(
            Finding("5 consistency", FAIL, "needs_login is set while the lock is held")
        )
    return findings


def _cmd(args: list[str]) -> str | None:
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, read-only
            args, capture_output=True, text=True, timeout=15, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def check_worker_process() -> list[Finding]:
    status = _cmd(["systemctl", "--user", "status", _UNIT, "--no-pager"])
    if status is None:
        return [Finding("6 worker process", SKIP, "systemctl --user not available on this host")]
    active = "Active: active (running)" in status
    journal = _cmd(["journalctl", "--user", "-u", _UNIT, "-n", "200", "--no-pager"]) or ""
    # A "warm shut down" is either the worker self-stopping (lock/session loss)
    # or the graceful response to a systemd stop. Only the former is a symptom;
    # subtract the systemd-initiated stops so a deploy/manual restart is not
    # miscounted as instability.
    warm = journal.count("warm shut down requested")
    systemd_stops = journal.count("Stopping translog")
    self_stops = max(0, warm - systemd_stops)
    reconnects = journal.count("could not connect to Redis")
    listening = journal.rfind("Listening on") > journal.rfind("Started ")
    return [
        Finding(
            "6 worker active",
            PASS if active else FAIL,
            "active (running)" if active else "not running",
        ),
        Finding(
            "6 journal(200)",
            WARN if (self_stops or reconnects > 3) else PASS,
            f"self-stops={self_stops}, redis-reconnects={reconnects}",
        ),
        Finding(
            "7 last-start auth",
            PASS if listening else WARN,
            "reached 'Listening on'" if listening else "no 'Listening on' after last start",
        ),
    ]


def check_clock() -> list[Finding]:
    out = _cmd(["timedatectl"])
    if out is None:
        return [Finding("10 clock", SKIP, "timedatectl not available")]
    synced = "System clock synchronized: yes" in out
    return [
        Finding("10 clock", PASS if synced else FAIL, "synchronized" if synced else "NOT synced")
    ]


def _authed_state(url: str, token: str) -> dict[str, Any]:
    """Log in with the token and GET /api/live/state. Read-only. The token is
    sent in the POST body, never logged or placed on a command line."""
    body = json.dumps({"password": token}).encode("utf-8")
    login = urllib.request.Request(  # noqa: S310 - fixed https dashboard URL
        f"{url}/login", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(login, timeout=15) as resp:  # noqa: S310
        cookie = (resp.headers.get("Set-Cookie") or "").split(";", 1)[0]
    if not cookie:
        raise RuntimeError("login did not return a session cookie (token rejected?)")
    state = urllib.request.Request(f"{url}/api/live/state", headers={"Cookie": cookie})  # noqa: S310
    with urllib.request.urlopen(state, timeout=30) as resp:  # noqa: S310
        data: Any = json.loads(resp.read() or b"{}")
    return data if isinstance(data, dict) else {}


def _dashboard_findings(
    snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
    stale_operator_hours: float = 24.0,
    stale_clarification_hours: float = 72.0,
) -> list[Finding]:
    """Checks 8 and 9 from a /api/live/state snapshot. Pure — no I/O — so it is
    unit-tested with a fixture snapshot."""
    from collections import Counter

    from translog_quote.domain.workflow import TERMINAL_STATES

    terminal = {s.value for s in TERMINAL_STATES}
    requests = snapshot.get("requests", [])
    demo = snapshot.get("demonstration", {})
    operations = demo.get("startup_mode") == "operations"
    counts = Counter(r.get("status", {}).get("state", "?") for r in requests)
    gt_holds = [r["request_id"] for r in requests if r.get("goods_type_hold")]
    clar = [r["request_id"] for r in requests if r.get("awaiting_clarification")]
    appr = [r["request_id"] for r in requests if r.get("awaiting_decision")]
    # Non-terminal with NOTHING pending: no clarification draft, no in-flight
    # job, no goods-type hold, no approval. Those are silently stuck and want a
    # look. (The snapshot lists active/followed requests; settled ones are out
    # of its scope — terminal counts need the store, check 8's Render path.)
    stuck = [
        r["request_id"]
        for r in requests
        if r.get("status", {}).get("state") not in terminal
        and not r.get("awaiting_clarification")
        and not r.get("rate_search_pending")
        and not r.get("goods_type_hold")
        and not r.get("awaiting_decision")
    ]
    # An empty active list is not automatically healthy: after a deploy it is
    # exactly the symptom this whole change addresses, so it is reported with
    # the cutoff rather than as a bare PASS.
    if counts:
        states_detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    else:
        started = demo.get("started_at")
        watermark = demo.get("last_poll_watermark")
        states_detail = f"0 active requests (demonstration started at {started}"
        states_detail += f", watermark {watermark})" if operations else ")"
    findings = [
        Finding("8 states (active)", PASS, states_detail),
        Finding(
            "8 holds",
            PASS,
            f"goods-type={gt_holds or 0}, clarification={clar or 0}, approval={appr or 0}",
        ),
        Finding(
            "8 stuck (nothing pending)",
            WARN if stuck else PASS,
            ", ".join(stuck) if stuck else "none",
        ),
    ]
    if operations:
        findings.append(
            _stale_finding(requests, terminal, now, stale_operator_hours, stale_clarification_hours)
        )
    poll = snapshot.get("poll", {})
    last, err = poll.get("last_checked_at"), poll.get("error")
    findings.append(
        Finding(
            "9 mailbox poll",
            FAIL if err else (WARN if last is None else PASS),
            f"last_checked_at={last}, error={err}",
        )
    )
    return findings


def _stale_finding(
    requests: list[dict[str, Any]],
    terminal: set[str],
    now: datetime | None,
    operator_hours: float,
    clarification_hours: float,
) -> Finding:
    """A WARN listing non-terminal requests older than their state's threshold.

    ``clarification_sent`` waits on a *client* and gets the longer window; every
    other non-terminal state is operator-owned and gets the shorter one. A row
    with no known first-seen time is skipped rather than guessed at."""
    when = now if now is not None else datetime.now(UTC)
    stale: list[str] = []
    for r in requests:
        state = r.get("status", {}).get("state")
        if state in terminal:
            continue
        seen = r.get("first_seen_at")
        if not seen:
            continue
        try:
            age_hours = (when - datetime.fromisoformat(seen)).total_seconds() / 3600
        except ValueError:
            continue
        limit = clarification_hours if state == "clarification_sent" else operator_hours
        if age_hours > limit:
            stale.append(f"{r['request_id']} ({state}, {age_hours:.0f}h)")
    return Finding(
        "8 stale (operations)",
        WARN if stale else PASS,
        ", ".join(stale) if stale else "none over threshold",
    )


def check_dashboard(url: str, token: str, *, settings: Any = None) -> list[Finding]:
    try:
        snapshot = _authed_state(url, token)
    except Exception as exc:  # noqa: BLE001
        return [Finding("8-9 dashboard", FAIL, f"authenticated API call failed: {exc}")]
    if settings is None:
        return _dashboard_findings(snapshot)
    return _dashboard_findings(
        snapshot,
        stale_operator_hours=settings.demo.stale_operator_hours,
        stale_clarification_hours=settings.demo.stale_clarification_hours,
    )


def _needs_approval() -> list[Finding]:
    return [
        Finding("8 request state", NEEDS_APPROVAL, "re-run with --token (token from env)"),
        Finding("9 mailbox polling", NEEDS_APPROVAL, "re-run with --token"),
    ]


def collect(*, url: str, env_file: str | None, token: str | None = None) -> list[Finding]:
    settings = bootstrap.load_settings(env_file) if env_file else bootstrap.load_settings()
    findings = check_render(url)
    try:
        conn = build_redis(settings)
        conn.ping()
        findings += check_redis(conn)
        findings += check_queue(
            conn, settings.queue.rate_search_queue, settings.queue.job_timeout_seconds
        )
        findings += check_keys(conn, settings.queue.worker_lock_key)
    except Exception as exc:  # noqa: BLE001
        findings.append(Finding("3-5 redis", FAIL, f"could not reach Redis: {exc}"))
    findings += check_worker_process()
    findings += check_clock()
    findings += check_dashboard(url, token, settings=settings) if token else _needs_approval()
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Translog pipeline health check.")
    parser.add_argument("--url", default=_DEFAULT_URL, help="dashboard base URL")
    parser.add_argument("--env-file", default=None, help="settings env file (e.g. .env.worker)")
    parser.add_argument(
        "--token",
        action="store_true",
        help="run checks 8-9 via GET /api/live/state, reading the dashboard token from "
        "$TRANSLOG_DASHBOARD_TOKEN (never from the command line, never printed)",
    )
    parser.add_argument("--token-env", default="TRANSLOG_DASHBOARD_TOKEN")
    args = parser.parse_args(argv)
    token = os.environ.get(args.token_env) if args.token else None
    if args.token and not token:
        print(f"--token given but ${args.token_env} is not set; skipping checks 8-9.")
    findings = collect(url=args.url, env_file=args.env_file, token=token)
    print(render(findings))
    return exit_code(findings)


def render(findings: list[Finding]) -> str:
    width = max((len(f.check) for f in findings), default=4)
    lines = [f"{'CHECK'.ljust(width)}  VERDICT  DETAIL", f"{'-' * width}  -------  ------"]
    lines += [f"{f.check.ljust(width)}  {f.verdict.ljust(7)}  {f.detail}" for f in findings]
    fails = sum(f.verdict == FAIL for f in findings)
    warns = sum(f.verdict == WARN for f in findings)
    lines.append(f"\n{fails} FAIL, {warns} WARN, {len(findings)} checks")
    return "\n".join(lines)


def exit_code(findings: list[Finding]) -> int:
    return 1 if any(f.verdict == FAIL for f in findings) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
