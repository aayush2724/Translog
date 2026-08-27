"""RealWebCargoAdapter — deliberately not implemented.

This file exists to hold the boundary and to say precisely why there is no code
behind it. Writing a plausible integration here would be worse than writing
none: it would look finished, and it would be wrong in ways that only show up
against a live account.

**What is missing, and why nothing was written:**

1. **No published API contract.** The only WebCargo material in this project is
   `docs/reference/cargo-automation-workflow.pdf`, which describes endpoints
   *captured from browser DevTools* — its own words — rather than a documented
   interface. Implementing against a reverse-engineered private endpoint is not
   something to do from a PDF, and the endpoints are not reproduced here.

2. **No confirmed authentication mechanism.** The specification describes a
   stored username and password exchanged for a session cookie, with no
   documented login endpoint, token lifetime, refresh rule or error contract
   (AMB-16).

3. **No transit-time source (AMB-1).** The response shape the specification
   records carries carrier, product, total, currency and a liquids flag — and no
   transit time, flight date or routing. Transit is the primary ranking key, so
   the adapter cannot rank anything even if it could authenticate. See
   `mapper.py`, where that gap is an executable blocker rather than a comment.

4. **No confirmed eligibility rules (AMB-3).** One liquids restriction is
   documented; nothing maps a commodity to a physical form.

Until a real API contract exists, `MockWebCargoAdapter` serves the demo and says
so in its output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.errors import PermanentFailure

if TYPE_CHECKING:
    from translog_quote.domain.rates import RateQuery, RateSearchResult

ADAPTER_ID = "webcargo"

_UNAVAILABLE = (
    "The real WebCargo adapter is not implemented. No published API contract, "
    "authentication mechanism, or transit-time source has been provided — see "
    "adapters/webcargo/real.py for what is missing. Use "
    "TRANSLOG_WEBCARGO__MODE=mock for the demo."
)


class RealWebCargoAdapter:
    """A `RateSearchPort` that refuses, loudly and with the reason."""

    adapter_id = ADAPTER_ID

    def __init__(self, *, base_url: str | None = None) -> None:
        # Credentials are deliberately not accepted here. There is nothing to
        # authenticate against, and a constructor that took a password would
        # invite one to be configured for an integration that does not exist.
        self._base_url = base_url

    def search(self, query: RateQuery) -> RateSearchResult:
        raise PermanentFailure(_UNAVAILABLE)
