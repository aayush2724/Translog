"""The goods-type decision: General Cargo only when unambiguous, else an
operator hold. Never a silent default. Pure domain logic, no I/O.
"""

from __future__ import annotations

from translog_quote.domain.goods_type import (
    catalog_configured,
    decide_goods_type,
    effective_catalog,
    record_fingerprint,
)

_LABEL = "0000 - General Cargo"
_SPECIAL = ("battery", "lithium", "fresh", "perishable", "dry ice")


def _decide(
    *,
    commodity: str = "Cotton garments",
    cargo_type: str | None = "general cargo",
    is_chemical: bool | None = False,
    special: tuple[str, ...] = _SPECIAL,
):
    return decide_goods_type(
        commodity=commodity,
        cargo_type=cargo_type,
        is_chemical=is_chemical,
        general_cargo_label=_LABEL,
        special_handling=special,
    )


# --- General Cargo acceptance -----------------------------------------------------


def test_both_live_cases_resolve_to_general_cargo() -> None:
    # The two enquiries that failed live: general/non-haz cargo, not chemical.
    a = _decide(commodity="Cotton garments (general cargo)", cargo_type="general cargo")
    b = _decide(commodity="Auto spare parts", cargo_type="non hazardous")
    assert a.goods_type == _LABEL and a.source == "rule"
    assert b.goods_type == _LABEL and b.source == "rule"


def test_general_cargo_non_hazardous_phrasing_is_accepted() -> None:
    """Item-2 normalisation: '-,()/' flatten to spaces before exact membership."""
    assert _decide(cargo_type="General cargo (non-hazardous)").goods_type == _LABEL
    assert _decide(cargo_type="non-hazardous").goods_type == _LABEL
    assert _decide(cargo_type="Non Hazardous / General Cargo").goods_type == _LABEL


# --- holds (never a silent default) -----------------------------------------------


def test_chemical_true_or_unknown_holds() -> None:
    assert _decide(is_chemical=True).held
    assert _decide(is_chemical=None).held  # None is not good enough


def test_unclear_or_absent_cargo_type_holds() -> None:
    assert _decide(cargo_type=None).held
    assert _decide(cargo_type="").held
    assert _decide(cargo_type="machinery").held


def test_near_miss_cargo_type_is_not_general_cargo() -> None:
    """Exact membership only — no substring/keyword guessing."""
    assert _decide(cargo_type="general cargonx").held
    assert _decide(cargo_type="gen").held
    assert _decide(cargo_type="general").held  # dropped from the accept set
    assert _decide(cargo_type="hazardous general cargo").held


def test_special_handling_word_hit_holds() -> None:
    assert _decide(commodity="lithium battery pack").held
    assert _decide(commodity="assorted dry ice shipment").held


def test_special_handling_is_word_matching_not_substring() -> None:
    """'fresh' hits 'fresh mangoes' but not 'freshly pressed'."""
    assert _decide(commodity="fresh mangoes").held
    assert _decide(commodity="freshly pressed juice concentrate").goods_type == _LABEL


def test_fresh_mangoes_with_fresh_listed_holds() -> None:
    # User case: special word on the commodity holds even when cargo_type is fine.
    assert _decide(
        commodity="Fresh mangoes (non-hazardous)", cargo_type="non hazardous"
    ).held


def test_empty_special_list_holds_even_general_cargo() -> None:
    # Rule OFF until the business configures the special-handling list.
    assert _decide(cargo_type="general cargo", special=()).held


# --- catalog helpers --------------------------------------------------------------


def test_effective_catalog_includes_general_cargo_first_and_skips_blanks() -> None:
    assert effective_catalog((), _LABEL) == (_LABEL,)
    assert effective_catalog(("1234 - Machinery",), _LABEL) == (_LABEL, "1234 - Machinery")
    # de-dupes the general-cargo label if the business also listed it
    assert effective_catalog((_LABEL, "9 - X"), _LABEL) == (_LABEL, "9 - X")
    # a blank general-cargo label is never offered; catalog entries still are
    assert effective_catalog(("1234 - Machinery",), "") == ("1234 - Machinery",)
    assert effective_catalog((), "   ") == ()


def test_catalog_configured_is_true_whenever_a_valid_label_exists() -> None:
    # The general-cargo label alone is enough — the effective catalog contains
    # it, so the picker is configured even before the business adds a catalog.
    assert catalog_configured((), _LABEL) is True
    assert catalog_configured(("1234 - Machinery",), _LABEL) is True
    # 'not configured' only when there is nothing valid to offer at all:
    assert catalog_configured((), "") is False
    assert catalog_configured((), "   ") is False
    # a catalog entry alone still configures it, even with a blank label:
    assert catalog_configured(("1234 - Machinery",), "") is True


# --- fingerprint ------------------------------------------------------------------


def test_fingerprint_changes_when_cargo_facts_change() -> None:
    base = record_fingerprint("Auto spare parts", "general cargo", False)
    assert base == record_fingerprint("auto spare parts", "GENERAL CARGO", False)  # normalised
    assert base != record_fingerprint("Auto spare parts", "hazardous", False)
    assert base != record_fingerprint("Cotton garments", "general cargo", False)
    assert base != record_fingerprint("Auto spare parts", "general cargo", True)
