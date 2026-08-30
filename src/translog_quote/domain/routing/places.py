"""Place names, tidied — never translated into airport codes.

This module used to hold `DEMO_LANES`, a nineteen-entry table mapping place
names to IATA codes, and `resolve_iata`, which refused anything absent from it.
The refusal was right and is kept, one layer out; the table was not, and is
gone.

Why it had to go, stated plainly so it is not reintroduced:

- It sat in `domain/`, the layer that may not know what a provider is, and
  encoded one provider's identifiers.
- `build_query` consulted it on every request regardless of which adapter was
  configured, so it gated production behind a list written for a demo.
- Neither simulated adapter needed what it produced. The mock returns the same
  rates for every query; the demo provider echoes the codes into its payload and
  prices from chargeable weight. The whitelist stopped enquiries in front of
  adapters that ignored its output.

What replaces it is a *port* — `ports.routing.LocationResolverPort` — satisfied
by an adapter that can actually resolve, or one that honestly cannot. What
stays here is the part that was always domain logic: making two spellings of
the same place comparable, without deciding what either one means.

Nothing in this module maps, infers, abbreviates or guesses.
"""

from __future__ import annotations

import re

_PARENTHETICAL = re.compile(r"\s*\(.*?\)\s*")
_WHITESPACE = re.compile(r"\s+")


def normalise_place(place: str) -> str:
    """A place name with the decoration clients add stripped off.

    Handles the shapes real enquiries arrive in — "Bahrain (Hidd Industrial
    Area)", "Dubai Airport", "  Mumbai  " — and leaves everything else exactly
    as written. In particular it keeps country and region qualifiers: "Dubai,
    UAE" normalises to "dubai, uae", not to "dubai". Dropping the qualifier
    would be a judgement about what part of the name identifies the place, and
    that judgement belongs to whatever can actually resolve it.

    Returns lowercase, for comparison. The client's original wording is what is
    carried forward and displayed; this is only ever the comparison key.
    """
    text = _PARENTHETICAL.sub(" ", place)
    text = _WHITESPACE.sub(" ", text).strip().lower()
    trimmed = text.removesuffix(" airport").strip()
    return trimmed or text


def is_nameable(place: str | None) -> bool:
    """Whether this is a place name at all, as opposed to nothing.

    The one judgement made here, and it is deliberately the weakest possible
    one: a resolver cannot be asked about the empty string. Anything a person
    actually wrote is passed along to something that can look it up — this
    function exists to catch absence, never to filter by plausibility.
    """
    return bool(place and place.strip() and normalise_place(place))
