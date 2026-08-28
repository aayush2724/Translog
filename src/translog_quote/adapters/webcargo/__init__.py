"""adapters.webcargo

Implements RateSearchPort.

    MockWebCargoAdapter  — deterministic fixture rates, identical for every
                           query. Used by the tests. Makes no network call.
    DemoRateProvider     — simulated WebCargo-shaped rates, priced from the
                           shipment actually being quoted. Used by the demo.
                           Makes no network call.
    RealWebCargoAdapter  — refuses, with the reason. No published API contract,
                           no confirmed authentication, no transit-time source.
    RealRateMapper       — the documented field mapping, with transit left as an
                           executable blocker (AMB-1).

Nothing here presents invented data as real WebCargo data: both simulating
adapters flag every result ``is_simulated=True``.
"""

from translog_quote.adapters.webcargo.demo import (
    DEMO_CARRIERS,
    DISCLOSURE,
    DemoRateProvider,
    map_rows,
    simulate_response,
)
from translog_quote.adapters.webcargo.mapper import RealRateMapper
from translog_quote.adapters.webcargo.mock import DEMO_RATES, MockWebCargoAdapter
from translog_quote.adapters.webcargo.real import RealWebCargoAdapter

__all__ = [
    "DEMO_CARRIERS",
    "DEMO_RATES",
    "DISCLOSURE",
    "DemoRateProvider",
    "MockWebCargoAdapter",
    "RealRateMapper",
    "RealWebCargoAdapter",
    "map_rows",
    "simulate_response",
]
