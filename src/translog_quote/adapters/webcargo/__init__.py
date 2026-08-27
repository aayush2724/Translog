"""adapters.webcargo

Implements RateSearchPort.

    MockWebCargoAdapter  — deterministic fixture rates for the demo. Makes no
                           network call, and says so in every result it returns.
    RealWebCargoAdapter  — refuses, with the reason. No published API contract,
                           no confirmed authentication, no transit-time source.
    RealRateMapper       — the documented field mapping, with transit left as an
                           executable blocker (AMB-1).

Nothing here presents fixture data as real WebCargo data.
"""

from translog_quote.adapters.webcargo.mapper import RealRateMapper
from translog_quote.adapters.webcargo.mock import DEMO_RATES, MockWebCargoAdapter
from translog_quote.adapters.webcargo.real import RealWebCargoAdapter

__all__ = [
    "DEMO_RATES",
    "MockWebCargoAdapter",
    "RealRateMapper",
    "RealWebCargoAdapter",
]
