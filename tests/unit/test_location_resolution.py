"""Deterministic natural-location -> IATA resolution for the browser path.

Clients write "Delhi, India" / "Singapore" / "Bangalore"; WebCargo's autocomplete
wants the code. The resolver maps known places from a reviewed table, lets an
explicit code through, and refuses (clarifies) anything it cannot resolve —
never a guess. Explicit-code and demo behaviour stay exactly as they were.
"""

from __future__ import annotations

import pytest

from translog_quote import bootstrap
from translog_quote.adapters.routing import CanonicalLocationResolver, StatedLocationResolver
from translog_quote.config import Settings, WebCargoMode
from translog_quote.errors import UnresolvedLocation


@pytest.mark.parametrize(
    ("stated", "code"),
    [
        ("Delhi, India", "DEL"),
        ("Singapore", "SIN"),
        ("Bangalore", "BLR"),
        ("Bangalore, India", "BLR"),
        ("Mumbai", "BOM"),
        ("Mumbai, India", "BOM"),
        ("Chennai, India", "MAA"),
        ("Hyderabad, India", "HYD"),
        ("Kolkata, India", "CCU"),
        ("Pune, India", "PNQ"),
    ],
)
def test_natural_names_resolve_to_canonical_codes(stated: str, code: str) -> None:
    ref = CanonicalLocationResolver().resolve(stated)
    assert ref.code == code
    assert ref.resolved_by == "canonical"
    assert ref.stated == stated  # the client's original wording is preserved
    assert ref.display == code  # what the WebCargo autocomplete receives


def test_explicit_airport_code_passes_through() -> None:
    ref = CanonicalLocationResolver().resolve("DEL")
    assert ref.code == "DEL"
    assert ref.display == "DEL"


def test_parenthesised_code_is_used_directly() -> None:
    ref = CanonicalLocationResolver().resolve("Delhi (DEL)")
    assert ref.code == "DEL"


@pytest.mark.parametrize(
    ("stated", "code"),
    [
        ("DXB", "DXB"),  # a bare explicit code
        ("Dubai International Airport (DXB), UAE", "DXB"),  # code parenthesised, country after
        ("Dubai (DXB)", "DXB"),  # code parenthesised at the end
        ("dubai international airport (dxb)", "DXB"),  # lower-case parenthesised code
    ],
)
def test_an_explicit_code_resolves_even_with_text_after_it(stated: str, code: str) -> None:
    """Regression: a parenthesised code followed by a country or airport name
    ("… (DXB), UAE") was hidden by an end-of-string anchor and wrongly sent to a
    clarification. An explicit code the client gives must be used directly."""
    ref = CanonicalLocationResolver().resolve(stated)
    assert ref.code == code
    assert ref.resolved_by == "canonical"


def test_a_place_without_a_code_or_table_entry_still_clarifies() -> None:
    """The fix is narrow: a stated place with no explicit code and not in the
    reviewed table (e.g. "Dubai, UAE") still refuses rather than guessing."""
    with pytest.raises(UnresolvedLocation):
        CanonicalLocationResolver().resolve("Dubai, UAE")


def test_ambiguous_or_unknown_location_is_refused_not_guessed() -> None:
    with pytest.raises(UnresolvedLocation):
        CanonicalLocationResolver().resolve("some village nobody tabulated")
    # A three-letter city in normal case is NOT mistaken for a code.
    with pytest.raises(UnresolvedLocation):
        CanonicalLocationResolver().resolve("Goa")


def test_existing_stated_resolver_behaviour_is_unchanged() -> None:
    # The demo/simulated resolver still carries wording forward with no code.
    ref = StatedLocationResolver().resolve("Tokyo, Japan")
    assert ref.stated == "Tokyo, Japan"
    assert ref.code is None
    assert ref.display == "Tokyo, Japan"


def test_bootstrap_selects_canonical_only_in_browser_mode() -> None:
    browser = Settings(webcargo={"mode": WebCargoMode.BROWSER})
    demo = Settings(webcargo={"mode": WebCargoMode.DEMO})
    assert isinstance(bootstrap.build_location_resolver(browser), CanonicalLocationResolver)
    assert isinstance(bootstrap.build_location_resolver(demo), StatedLocationResolver)
