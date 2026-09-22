"""A minimal in-memory Redis double for the durable-store tests.

Not a pytest module (no ``test_`` prefix, so it is not collected). It implements
only the commands ``RedisStore`` / ``RedisDemonstrationStore`` / ``RedisAuditLog``
use — ``hset``/``hgetall``, ``get``/``set``, ``rpush``/``lrange`` and a
transactional ``pipeline`` — and returns ``bytes`` for values and hash fields,
exactly as ``redis-py`` does when the client is built without ``decode_responses``
(which is how ``build_redis`` builds it). A real ``fakeredis`` dependency is not
added: the project already hand-rolls a queue-specific fake, and these few
commands are cheaper to model directly than to take on a dependency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


def _b(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return str(value).encode("utf-8")


class FakeRedis:
    """A tiny subset of the redis-py client, in memory. One instance = one DB, so
    passing the same instance to a second store models a process restart."""

    def __init__(self) -> None:
        self._hashes: dict[str, dict[bytes, bytes]] = {}
        self._strings: dict[str, bytes] = {}
        self._lists: dict[str, list[bytes]] = {}

    # --- hashes (requests / threads) ---
    def hset(self, name: str, key: object, value: object) -> int:
        created = 0 if _b(key) in self._hashes.get(name, {}) else 1
        self._hashes.setdefault(name, {})[_b(key)] = _b(value)
        return created

    def hgetall(self, name: str) -> dict[bytes, bytes]:
        return dict(self._hashes.get(name, {}))

    # --- strings (watermark) ---
    def get(self, name: str) -> bytes | None:
        return self._strings.get(name)

    def set(self, name: str, value: object) -> bool:
        self._strings[name] = _b(value)
        return True

    # --- lists (audit) ---
    def rpush(self, name: str, *values: object) -> int:
        bucket = self._lists.setdefault(name, [])
        bucket.extend(_b(v) for v in values)
        return len(bucket)

    def lrange(self, name: str, start: int, end: int) -> list[bytes]:
        bucket = self._lists.get(name, [])
        if end == -1:
            return list(bucket[start:])
        return list(bucket[start : end + 1])

    # --- transactions (atomic request+thread commit) ---
    def pipeline(self, transaction: bool = True) -> _FakePipeline:  # noqa: FBT001, FBT002
        return _FakePipeline(self)


class _FakePipeline:
    """Buffers ``hset`` calls and applies them all on ``execute`` — a stand-in for
    MULTI/EXEC. In this single-threaded fake, applying them together on execute is
    exactly the atomicity the real transaction provides."""

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[str, object, object]] = []

    def hset(self, name: str, key: object, value: object) -> _FakePipeline:
        self._ops.append((name, key, value))
        return self

    def execute(self) -> list[int]:
        results = [self._redis.hset(name, key, value) for name, key, value in self._ops]
        self._ops = []
        return results


class FailingPipeline(_FakePipeline):
    """A pipeline whose ``execute`` raises, to prove an atomic commit leaves
    nothing behind (neither request nor thread) and the cache unchanged."""

    def execute(self) -> list[int]:
        raise RuntimeError("simulated Redis EXEC failure")


class FailingCommitRedis(FakeRedis):
    """A FakeRedis whose transactional pipeline fails on execute."""

    def pipeline(self, transaction: bool = True) -> _FakePipeline:  # noqa: FBT001, FBT002
        return FailingPipeline(self)


def keys_of(redis: FakeRedis) -> Iterable[str]:
    """Every key this fake currently holds, across hashes/strings/lists."""
    return (*redis._hashes.keys(), *redis._strings.keys(), *redis._lists.keys())
