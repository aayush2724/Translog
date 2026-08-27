"""Place name to IATA airport code."""

from __future__ import annotations

import re

#: Demo lanes only. Deliberately small: every entry here is one a person checked,
#: and a short table that fails loudly beats a long one that is quietly wrong.
DEMO_LANES: dict[str, str] = {
    "ahmedabad": "AMD",
    "ahmedabad airport": "AMD",
    "amd": "AMD",
    "bahrain": "BAH",
    "bah": "BAH",
    "mumbai": "BOM",
    "bom": "BOM",
    "delhi": "DEL",
    "del": "DEL",
    "muscat": "MCT",
    "mct": "MCT",
    "mundra": "MUN",
    "singapore": "SIN",
    "sin": "SIN",
    "dammam": "DMM",
    "dmm": "DMM",
    "jebel ali": "DXB",
    "dubai": "DXB",
    "dxb": "DXB",
}


class UnknownPlace(ValueError):
    """The place is not in the lookup, so no code can be produced.

    Deliberately an error rather than a fallback. Returning a nearby or
    plausible airport would produce rates for a lane nobody asked about.
    """


_PARENTHETICAL = re.compile(r"\s*\(.*?\)\s*")


def _normalise(place: str) -> str:
    """Trim the decoration clients add: "Bahrain (Hidd Industrial Area)"."""
    text = _PARENTHETICAL.sub(" ", place)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text.removesuffix(" airport").strip() or text


def resolve_iata(place: str | None) -> str:
    """The IATA code for a place the client named.

    Raises `UnknownPlace` for anything absent or unlisted, which is what keeps a
    wrong lane out of a real rate search.
    """
    if place is None or not place.strip():
        raise UnknownPlace("no place given")

    key = _normalise(place)
    if key in DEMO_LANES:
        return DEMO_LANES[key]

    # The bare form, in case the suffix strip removed something meaningful.
    bare = re.sub(r"\s+", " ", _PARENTHETICAL.sub(" ", place)).strip().lower()
    if bare in DEMO_LANES:
        return DEMO_LANES[bare]

    raise UnknownPlace(
        f"{place!r} is not in the demo lane table; add it deliberately "
        "rather than letting a rate search run against a guessed airport"
    )
