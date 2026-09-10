"""adapters.webcargo

Implements RateSearchPort.

    MockWebCargoAdapter  — deterministic fixture rates, identical for every
                           query. Used by the tests. Makes no network call.
    DemoRateProvider     — simulated WebCargo-shaped rates, priced from the
                           shipment actually being quoted. Used by the demo.
                           Makes no network call.

The real provider path is the browser adapter (WebCargo has no published API
contract), which lives in `adapters.webcargo.browser` and extracts only what
the WebCargo UI actually states.

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
from translog_quote.adapters.webcargo.mock import DEMO_RATES, MockWebCargoAdapter

__all__ = [
    "DEMO_CARRIERS",
    "DEMO_RATES",
    "DISCLOSURE",
    "DemoRateProvider",
    "MockWebCargoAdapter",
    "map_rows",
    "simulate_response",
]
