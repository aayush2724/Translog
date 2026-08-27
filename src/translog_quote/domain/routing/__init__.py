"""Resolving a place the client named into the airport code a rate query needs.

A deterministic lookup, not a model's job and not a guess. A hallucinated
airport code is a silent wrong answer: the search succeeds, rates come back, and
they are for the wrong lane (AMB-9).

The table covers the lanes this demo uses and nothing more. An unknown place
raises rather than resolving to something plausible.
"""

from translog_quote.domain.routing.iata import (
    DEMO_LANES,
    UnknownPlace,
    resolve_iata,
)

__all__ = ["DEMO_LANES", "UnknownPlace", "resolve_iata"]
