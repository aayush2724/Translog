"""``deploy/railway/`` stays consistent with the worker's runtime contract.

The Railway files are configuration that executes: a drift between the
entrypoint's restart wait and the Redis lock TTL, or between the pinned
Playwright wheel and the base image's browsers, would surface only as a
production restart loop or a browser that fails to launch. These tests pin
the invariants the systemd unit documents, against the same settings defaults
the worker reads.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from translog_quote.config.settings import QueueSettings

ROOT = Path(__file__).resolve().parents[2]
RAILWAY = ROOT / "deploy" / "railway"
WEB_CONFIG = RAILWAY / "web.railway.json"
WORKER_CONFIG = RAILWAY / "worker.railway.json"
ENTRYPOINTS = (RAILWAY / "web-entrypoint.sh", RAILWAY / "worker-entrypoint.sh")
HELPERS = (RAILWAY / "pack-secret-files.sh", RAILWAY / "pack-browser-profile.sh")

#: The systemd unit's TimeoutStopSec: how long a warm shutdown may take.
STOP_TIMEOUT_SECONDS = 200


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def test_both_services_build_from_their_own_dockerfile_as_a_single_replica() -> None:
    for config in (WEB_CONFIG, WORKER_CONFIG):
        data = _load(config)
        assert data["build"]["builder"] == "DOCKERFILE", config.name
        assert (ROOT / data["build"]["dockerfilePath"]).is_file(), config.name
        assert data["deploy"]["numReplicas"] == 1, config.name
        assert data["deploy"]["restartPolicyType"] == "ON_FAILURE", config.name


def test_web_service_is_healthchecked_on_the_liveness_route() -> None:
    deploy = _load(WEB_CONFIG)["deploy"]
    assert deploy["healthcheckPath"] == "/health"


def test_worker_has_no_http_healthcheck_and_drains_longer_than_a_warm_shutdown() -> None:
    deploy = _load(WORKER_CONFIG)["deploy"]
    assert "healthcheckPath" not in deploy
    assert deploy["overlapSeconds"] == 0
    assert deploy["drainingSeconds"] >= STOP_TIMEOUT_SECONDS
    assert deploy["drainingSeconds"] > QueueSettings().worker_lock_ttl_seconds


def test_worker_entrypoint_waits_out_the_lock_ttl_before_a_restart() -> None:
    text = (RAILWAY / "worker-entrypoint.sh").read_text(encoding="utf-8")
    match = re.search(r"TRANSLOG_RESTART_WAIT_SECONDS:-(\d+)", text)
    assert match is not None
    assert int(match.group(1)) > QueueSettings().worker_lock_ttl_seconds


def test_worker_entrypoint_honours_the_exit_code_contract() -> None:
    text = (RAILWAY / "worker-entrypoint.sh").read_text(encoding="utf-8")
    assert "78)" in text, "needs-login must park, never restart-loop"
    assert "trap forward TERM INT" in text, "SIGTERM must reach the Python worker"
    assert "--login" in text, "login mode must reuse the worker's own --login"


def test_playwright_pin_matches_the_worker_base_image() -> None:
    pinned = re.search(r"^playwright==(\S+)", (RAILWAY / "constraints.txt").read_text(), re.M)
    assert pinned is not None
    dockerfile = (RAILWAY / "Dockerfile.worker").read_text(encoding="utf-8")
    assert f"mcr.microsoft.com/playwright/python:v{pinned.group(1)}-" in dockerfile


def test_scripts_are_executable_and_parse() -> None:
    bash = shutil.which("bash")
    for script in (*ENTRYPOINTS, *HELPERS):
        assert os.access(script, os.X_OK), script.name
        if bash is not None:
            subprocess.run([bash, "-n", str(script)], check=True)
    if bash is None:
        pytest.skip("bash not available; syntax check skipped")


def test_dockerignore_keeps_secrets_and_sessions_out_of_the_image() -> None:
    lines = {
        line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    }
    for required in (".secrets/", ".browser/", ".env", ".env.*", "docs/ai/"):
        assert required in lines, required
