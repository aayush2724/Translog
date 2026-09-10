"""Location resolution.

`StatedLocationResolver` carries the client's wording forward and attaches no
identifier, which is all the simulated providers need. The production path is
the WebCargo browser adapter's own location lookup — WebCargo's UI decides what
a place means on its network, and records itself as the resolver.

Nothing here infers a code from a place name.
"""

from translog_quote.adapters.routing.stated import StatedLocationResolver

__all__ = ["StatedLocationResolver"]
