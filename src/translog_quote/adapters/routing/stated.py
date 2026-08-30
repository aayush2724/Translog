"""StatedLocationResolver — accepts the place, supplies no code.

The resolver the simulated providers run behind. It accepts any place a person
actually wrote and returns it unchanged, with **no** identifier attached.

That "no" is the whole design, and it is not a limitation being tolerated — it
is the correct answer. The simulated providers do not use an identifier:
`MockWebCargoAdapter` returns the same rates for every query by construction,
and `DemoRateProvider` prices from chargeable weight and echoes whatever the
query carried. Nothing downstream reads a code. So there is nothing an
identifier here could be *for* except appearing on screen — and an identifier
invented to look convincing on screen is precisely the failure this system must
not have.

What it therefore does not do, in any circumstance: derive a code from a name,
abbreviate, take the first three letters, consult a table, or fall back to
another resolver. A demo that accepts "Tokyo, Japan" and shows "Tokyo, Japan"
is honest. One that accepts it and shows "TYO" is lying about having looked
something up.
"""

from __future__ import annotations

from translog_quote.domain.rates import LocationRef
from translog_quote.domain.routing import is_nameable
from translog_quote.errors import UnresolvedLocation

RESOLVER_ID = "stated"


class StatedLocationResolver:
    """A `LocationResolverPort` that carries the client's own wording forward."""

    resolver_id = RESOLVER_ID

    def resolve(self, place: str) -> LocationRef:
        """The place as stated. Refuses only absence, never unfamiliarity.

        There is no list to be absent from. The single rejection is the empty
        string, because a provider cannot be asked about nothing — and because
        an enquiry that stated no origin is an incomplete enquiry, which the
        validator has already caught by the time anything reaches here.
        """
        if not is_nameable(place):
            raise UnresolvedLocation(
                "No place was stated, so there is nothing to resolve. This is a "
                "missing shipment field, not an unrecognised location."
            )
        return LocationRef(stated=place.strip())
