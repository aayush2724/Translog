"""The location-resolution boundary."""

from __future__ import annotations

from typing import Protocol

from translog_quote.domain.rates import LocationRef


class LocationResolverPort(Protocol):
    """Turns a place the client named into something a provider can be asked about.

    This is the seam that used to be a nineteen-entry dictionary in `domain`.
    Making it a port is what lets the demo accept any place on earth while the
    production path insists on a real identifier from a real provider — without
    either of them knowing the other exists.

    Two rules bind every implementation:

    - **Never infer.** An implementation may return a code it obtained, or no
      code at all. Deriving one from the place name — by prefix, by similarity,
      by a shipped table — is forbidden, because a wrong airport produces a
      successful search of the wrong lane (AMB-9).
    - **Refuse by raising `UnresolvedLocation`**, which is a `PermanentFailure`
      and therefore already isolated per request by the callers. A resolver
      never returns a sentinel, an empty ref, or a nearest match.
    """

    resolver_id: str

    def resolve(self, place: str) -> LocationRef: ...
