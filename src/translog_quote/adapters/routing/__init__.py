"""Location resolution — implemented twice, for the two kinds of run.

`StatedLocationResolver` carries the client's wording forward and attaches no
identifier, which is all the simulated providers need. `WebCargoLocationResolver`
is the production path and asks the provider; it is unimplemented and refuses
rather than approximating.

Neither infers a code from a place name, and the second never falls back to the
first.
"""

from translog_quote.adapters.routing.stated import StatedLocationResolver
from translog_quote.adapters.routing.webcargo import WebCargoLocationResolver

__all__ = ["StatedLocationResolver", "WebCargoLocationResolver"]
