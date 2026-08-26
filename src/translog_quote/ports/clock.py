"""Time, as a dependency.

No module reads the wall clock directly. Demos are wired with a fixed clock, which
is one of the four sources of nondeterminism the design closes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class ClockPort(Protocol):
    def now(self) -> datetime: ...
