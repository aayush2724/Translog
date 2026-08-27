"""Clock implementations.

`FixedClock` never advances, which is what makes an audit trail diffable across
runs. `SystemClock` is the real one, and nothing in the demo uses it.
"""

from __future__ import annotations

from datetime import UTC, datetime

DEMO_EPOCH = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


class FixedClock:
    """Always returns the same moment. Demos that drift are not demonstrations."""

    def __init__(self, moment: datetime | None = None) -> None:
        self._moment = moment or DEMO_EPOCH

    def now(self) -> datetime:
        return self._moment


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
