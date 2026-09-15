"""CanonicalLocationResolver — a deterministic name → IATA code, or a refusal.

A real client writes a place the way people do — "Delhi, India", "Singapore",
"Bangalore" — and should never need to know an airport code. This resolver turns
that wording into the code WebCargo's own autocomplete recognises, **from a
reviewed table only** — never by inference, abbreviation, first-three-letters,
or any per-place branching. The table is data; the lookup is one dict access.

Three shapes are accepted without guessing:
  * an explicit code the client already gave ("DEL")            -> DEL
  * a code the client parenthesised ("Delhi (DEL)")             -> DEL
  * a known place name, country suffix folded ("Delhi, India")  -> DEL

Anything the table does not know is **not** guessed. It is refused with
`UnresolvedLocation`, which the interface surfaces as a request to state the
airport (e.g. "Delhi (DEL)") — a clarification, never a wrong code. A wrong code
is the worst failure this system can make; refusing is the correct answer.
"""

from __future__ import annotations

import re

from translog_quote.domain.rates import LocationRef
from translog_quote.domain.routing import is_nameable
from translog_quote.errors import UnresolvedLocation

RESOLVER_ID = "canonical"

#: Normalised place name -> IATA code. Data, reviewed by a human, never
#: inferred. Adding a city is one line; the resolver never branches per place.
CANONICAL_IATA: dict[str, str] = {
    "delhi": "DEL",
    "new delhi": "DEL",
    "mumbai": "BOM",
    "bombay": "BOM",
    "bangalore": "BLR",
    "bengaluru": "BLR",
    "chennai": "MAA",
    "madras": "MAA",
    "hyderabad": "HYD",
    "kolkata": "CCU",
    "calcutta": "CCU",
    "pune": "PNQ",
    "singapore": "SIN",
}

#: An explicit IATA code, as codes are printed: three upper-case letters. Case
#: matters — it keeps a three-letter city written in normal case ("Goa") from
#: being mistaken for a code, so that falls through to the table (and, if
#: unknown, to a clarification) rather than being sent as "GOA".
_EXPLICIT_CODE = re.compile(r"^[A-Z]{3}$")

#: A code the client parenthesised: "Delhi (DEL)", "Mumbai (BOM)".
_PARENTHESISED_CODE = re.compile(r"\(([A-Za-z]{3})\)\s*$")

#: A trailing country the client tacked on, folded before lookup.
_COUNTRY_SUFFIX = re.compile(r"\s*,\s*(?:india|in)\s*$", re.IGNORECASE)


def _normalise(place: str) -> str:
    """Fold to a comparable key: lower-case, no country suffix, single spaces."""
    key = _COUNTRY_SUFFIX.sub("", place.strip().lower())
    return re.sub(r"\s+", " ", key).strip()


class CanonicalLocationResolver:
    """A `LocationResolverPort`: known names -> IATA, explicit codes straight
    through, anything else -> `UnresolvedLocation` (a clarification, never a
    guess)."""

    resolver_id = RESOLVER_ID

    def resolve(self, place: str) -> LocationRef:
        if not is_nameable(place):
            raise UnresolvedLocation(
                "No place was stated, so there is nothing to resolve. This is a "
                "missing shipment field, not an unrecognised location."
            )
        stated = place.strip()

        parenthesised = _PARENTHESISED_CODE.search(stated)
        if parenthesised is not None:
            return LocationRef(
                stated=stated, code=parenthesised.group(1).upper(), resolved_by=RESOLVER_ID
            )
        if _EXPLICIT_CODE.match(stated):
            return LocationRef(stated=stated, code=stated, resolved_by=RESOLVER_ID)

        code = CANONICAL_IATA.get(_normalise(stated))
        if code is not None:
            return LocationRef(stated=stated, code=code, resolved_by=RESOLVER_ID)

        raise UnresolvedLocation(
            f"No airport could be resolved for {stated!r} without guessing. "
            "Please state it with its airport, for example 'Delhi (DEL)'."
        )
