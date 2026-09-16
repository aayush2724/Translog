"""Deciding a WebCargo Goods Type from a validated shipment — deterministically.

WebCargo's Goods Type is a controlled dropdown. The client's free-text commodity
is NEVER typed into it (that is what returned ``[]`` for every realistic
enquiry). Instead this decides an exact WebCargo label before the search is
enqueued:

- the reviewed General Cargo label, ONLY when the cargo is unambiguously
  general/non-hazardous (exact normalised ``cargo_type`` membership, ``is_chemical``
  explicitly ``False``, and no special-handling word in the commodity), and only
  while a special-handling list is configured;
- otherwise a *hold* — an operator picks the exact label from the reviewed
  catalog. Never a silent default.

Pure and side-effect free: it takes primitives (the config values the web layer
reads from ``Settings``), so the domain never imports configuration. The
free-text commodity stays on the record and in the quotation as the description;
it is not a Goods Type.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

#: cargo_type phrases that qualify as General Cargo, AFTER normalisation
#: (casefold; ``-,()/`` -> space; collapse whitespace). Reviewed and exact — no
#: substring or keyword guessing. "non-hazardous" -> "non hazardous",
#: "General cargo (non-hazardous)" -> "general cargo non hazardous", etc.
_ACCEPTED_CARGO_TYPES: frozenset[str] = frozenset(
    {
        "general cargo",
        "non hazardous",
        "non haz",
        "general cargo non hazardous",
        "non hazardous general cargo",
    }
)

#: Characters flattened to spaces before matching, so punctuation and bracketed
#: qualifiers do not defeat an otherwise-exact phrase.
_PUNCTUATION = str.maketrans({ch: " " for ch in "-,()/"})


def _normalise(text: str | None) -> str:
    """Casefold, flatten ``-,()/`` to spaces, collapse whitespace. Never guesses."""
    if not text:
        return ""
    return " ".join(text.casefold().translate(_PUNCTUATION).split())


def _tokens(text: str | None) -> list[str]:
    return _normalise(text).split()


def _has_special_handling(commodity: str, special_handling: tuple[str, ...]) -> bool:
    """Whether any special-handling entry appears as a whole word/phrase.

    WORD matching, not substring: an entry's tokens must appear as a contiguous
    run of the commodity's tokens, so "fresh" hits "fresh mangoes" but not
    "freshly". A false positive only routes to an operator (the safe direction),
    which is why word matching — not exact equality — is correct here.
    """
    tokens = _tokens(commodity)
    for entry in special_handling:
        needle = _tokens(entry)
        if not needle:
            continue
        span = len(needle)
        for start in range(len(tokens) - span + 1):
            if tokens[start : start + span] == needle:
                return True
    return False


@dataclass(frozen=True, slots=True)
class GoodsTypeDecision:
    """The Goods Type to search under, or a hold for an operator to decide."""

    goods_type: str | None
    """The exact WebCargo label to select, or ``None`` when held for an operator."""
    source: str | None
    """``"rule"`` when the General Cargo rule decided it; ``None`` when held."""

    @property
    def held(self) -> bool:
        return self.goods_type is None


def decide_goods_type(
    *,
    commodity: str,
    cargo_type: str | None,
    is_chemical: bool | None,
    general_cargo_label: str,
    special_handling: tuple[str, ...],
) -> GoodsTypeDecision:
    """Decide the Goods Type, or hold. Never a silent default.

    General Cargo requires ALL of: a configured special-handling list (empty
    disables the rule entirely — everything holds), ``cargo_type`` normalising
    to a reviewed accepted phrase, ``is_chemical`` explicitly ``False`` (``None``
    is not good enough), and no special-handling word in the commodity.
    """
    if not special_handling:
        # Rule OFF until the business enables the list: hold everything.
        return GoodsTypeDecision(goods_type=None, source=None)
    if is_chemical is not False:
        return GoodsTypeDecision(goods_type=None, source=None)
    if _normalise(cargo_type) not in _ACCEPTED_CARGO_TYPES:
        return GoodsTypeDecision(goods_type=None, source=None)
    if _has_special_handling(commodity, special_handling):
        return GoodsTypeDecision(goods_type=None, source=None)
    return GoodsTypeDecision(goods_type=general_cargo_label, source="rule")


def effective_catalog(catalog: tuple[str, ...], general_cargo_label: str) -> tuple[str, ...]:
    """The operator's pickable labels: the general-cargo label first (when it is
    a real, non-blank label), then the configured catalog, de-duplicated, order
    preserved. Blank/whitespace entries are never offered."""
    ordered: list[str] = []
    if general_cargo_label.strip():
        ordered.append(general_cargo_label)
    for label in catalog:
        if label.strip() and label not in ordered:
            ordered.append(label)
    return tuple(ordered)


def catalog_configured(catalog: tuple[str, ...], general_cargo_label: str) -> bool:
    """Whether there is at least one valid Goods Type an operator can pick.

    The general-cargo label always counts (it is a real WebCargo entry), so with
    any non-blank label the picker is configured — even before the business adds
    a catalog. 'Goods-type catalog not configured' therefore appears only in the
    degenerate case: a blank/invalid general-cargo label AND no catalog entries,
    i.e. nothing valid to offer at all."""
    return len(effective_catalog(catalog, general_cargo_label)) > 0


def record_fingerprint(
    commodity: str, cargo_type: str | None, is_chemical: bool | None
) -> str:
    """A stable fingerprint of the cargo facts an operator's pick was made
    against. If the record later changes (e.g. a client reply), the fingerprint
    changes and the pick is discarded — never applied to a different shipment."""
    basis = f"{_normalise(commodity)}\x1f{_normalise(cargo_type)}\x1f{is_chemical!r}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()
