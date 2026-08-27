"""adapters.clock

Implements ClockPort. FixedClock for demos and tests, SystemClock for real use.
"""

from translog_quote.adapters.clock.fixed import DEMO_EPOCH, FixedClock, SystemClock

__all__ = ["DEMO_EPOCH", "FixedClock", "SystemClock"]
