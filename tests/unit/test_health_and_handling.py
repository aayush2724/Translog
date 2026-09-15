"""Health routes and global error handling on the rate-search API.

Liveness needs no dependency; readiness reports the Redis dependency as a 200
or a 503 and never raises; an unhandled error becomes a 500 whose body carries
no internals.
"""

from __future__ import annotations

import redis
from fastapi.testclient import TestClient

from translog_quote.config import Settings
from translog_quote.interface.api.app import create_app


def _client() -> TestClient:
    return TestClient(create_app(Settings()), raise_server_exceptions=False)


class _FakeRedis:
    def __init__(self, *, ok: bool) -> None:
        self._ok = ok

    def ping(self) -> bool:
        if not self._ok:
            raise ConnectionError("Connection closed by server")
        return True


def test_health_liveness_needs_no_dependency() -> None:
    r = _client().get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readiness_is_503_when_redis_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(redis.Redis, "from_url", lambda *a, **k: _FakeRedis(ok=False))
    r = _client().get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not ready"
    assert body["redis"] == "unreachable"


def test_readiness_is_200_when_redis_reachable(monkeypatch) -> None:
    monkeypatch.setattr(redis.Redis, "from_url", lambda *a, **k: _FakeRedis(ok=True))
    r = _client().get("/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "redis": "ok"}


def test_unhandled_error_becomes_500_without_leaking_internals() -> None:
    app = create_app(Settings())

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail")

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/boom")
    assert r.status_code == 500
    assert r.json() == {"detail": "internal server error"}
    assert "secret internal detail" not in r.text
