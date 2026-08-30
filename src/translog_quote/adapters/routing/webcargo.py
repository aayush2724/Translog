"""WebCargoLocationResolver — the production path, and why it refuses today.

The real resolver asks WebCargo's own location lookup what a place is, and uses
what it answers. That is the only acceptable source of an identifier in
production: the provider decides what "Dubai, UAE" means on its own network,
and we record which mechanism said so.

It is not implemented, for exactly the reasons `real.py` gives for the search
endpoint: there is no published API contract for WebCargo, no confirmed
authentication mechanism (AMB-16), and the only material available describes
endpoints captured from browser DevTools rather than a documented interface. A
plausible implementation written from that would look finished and be wrong
against a live account.

**This class must never fall back.** Not to `StatedLocationResolver`, not to a
table, not to a code inferred from the name. The distinction between the two
resolvers is the distinction between a demo that may show a place name and a
production quotation that must not carry a made-up airport — collapsing them
would silently move demo behaviour into a real client's quotation, which is a
worse failure than refusing to run at all.

Refusing per request is the designed behaviour: `UnresolvedLocation` is a
`PermanentFailure`, so callers record it against the one request and carry on
with the rest of the mailbox.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.errors import UnresolvedLocation

if TYPE_CHECKING:
    from translog_quote.domain.rates import LocationRef

RESOLVER_ID = "webcargo"

_UNAVAILABLE = (
    "The real WebCargo location lookup is not implemented, so {place!r} cannot be "
    "resolved to a provider identifier. No published API contract or authentication "
    "mechanism has been provided — see adapters/routing/webcargo.py. This resolver "
    "will not fall back to a table or infer a code from the place name."
)


class WebCargoLocationResolver:
    """A `LocationResolverPort` that refuses, loudly and with the reason."""

    resolver_id = RESOLVER_ID

    def __init__(self, *, base_url: str | None = None) -> None:
        # No credentials, for the same reason `RealWebCargoAdapter` takes none:
        # there is nothing to authenticate against, and a constructor that
        # accepted a password would invite one to be configured for an
        # integration that does not exist.
        self._base_url = base_url

    def resolve(self, place: str) -> LocationRef:
        raise UnresolvedLocation(_UNAVAILABLE.format(place=place))
