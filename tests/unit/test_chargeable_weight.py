"""Chargeable weight: the arithmetic a rate lookup depends on.

Pure domain, no network — which is the point. These formulas are published and
unambiguous, so they belong in the codebase rather than behind someone's
service.

The case that matters is the second section: dense cargo hides the omission,
and light bulky cargo exposes it.
"""

from __future__ import annotations

import pytest

from translog_quote.domain.shipment import (
    IATA_AIR_KG_PER_CBM,
    CargoDimensions,
    WeightBasis,
    chargeable_weight,
    volume_cbm,
    volumetric_weight_kg,
)

#: The reference shipment's dimensions: 34 x 24 x 6 inches, carrying 500 kg.
REFERENCE = CargoDimensions(length=34, width=24, height=6)

#: A light, bulky carton — the shape that prices on volume, not mass.
BULKY = CargoDimensions(length=48, width=40, height=40)


# --- geometry -------------------------------------------------------------------


def test_volume_is_computed_in_cubic_metres() -> None:
    """34 x 24 x 6 in = 4896 cu in. One cubic inch is 0.0254^3 m3."""
    assert volume_cbm(REFERENCE) == pytest.approx(4896 * 0.0254**3)
    assert volume_cbm(REFERENCE) == pytest.approx(0.08023, abs=1e-5)


def test_a_one_metre_cube_is_one_cbm() -> None:
    """39.3701 inches to the metre — the unit conversion, checked end to end."""
    metre_cube = CargoDimensions(length=39.3701, width=39.3701, height=39.3701)

    assert volume_cbm(metre_cube) == pytest.approx(1.0, abs=1e-4)


def test_volumetric_weight_scales_with_the_dimensional_factor() -> None:
    at_167 = volumetric_weight_kg(REFERENCE, kg_per_cbm=167.0)
    at_334 = volumetric_weight_kg(REFERENCE, kg_per_cbm=334.0)

    assert at_334 == pytest.approx(at_167 * 2)


def test_the_iata_air_factor_is_available_but_never_assumed() -> None:
    """The constant exists so a caller can name it. Every function still
    demands the ratio explicitly."""
    assert IATA_AIR_KG_PER_CBM == 167.0
    with pytest.raises(TypeError):
        volumetric_weight_kg(REFERENCE)  # type: ignore[call-arg]


# --- the case that matters ------------------------------------------------------


def test_dense_cargo_charges_on_gross_weight() -> None:
    """The reference shipment is dense: ~0.08 cbm, ~13 kg volumetric against
    500 kg actual. Gross wins — which is exactly why the omission was easy to
    miss."""
    result = chargeable_weight(gross_kg=500.0, dimensions=REFERENCE, kg_per_cbm=IATA_AIR_KG_PER_CBM)

    assert result.basis is WeightBasis.GROSS
    assert result.chargeable_kg == 500.0
    assert result.volumetric_kg == pytest.approx(13.4, abs=0.1)
    assert result.volumetric_governs is False


def test_light_bulky_cargo_charges_on_volumetric_weight() -> None:
    """48 x 40 x 40 in is ~1.26 cbm — about 210 kg volumetric. A 30 kg
    shipment in that box is billed as 210 kg, and quoting 30 kg would
    under-price it sevenfold."""
    result = chargeable_weight(gross_kg=30.0, dimensions=BULKY, kg_per_cbm=IATA_AIR_KG_PER_CBM)

    assert result.basis is WeightBasis.VOLUMETRIC
    assert result.volumetric_governs is True
    assert result.chargeable_kg == result.volumetric_kg
    assert result.chargeable_kg > result.gross_kg


def test_both_inputs_are_kept_so_the_result_can_explain_itself() -> None:
    result = chargeable_weight(gross_kg=30.0, dimensions=BULKY, kg_per_cbm=167.0)

    assert result.gross_kg == 30.0
    assert result.volumetric_kg > 0
    assert result.kg_per_cbm == 167.0  # reproducible later


def test_a_tie_resolves_to_gross() -> None:
    """Equal weights mean no volumetric uplift; reporting one would imply a
    surcharge that does not exist."""
    volumetric = volumetric_weight_kg(REFERENCE, kg_per_cbm=167.0)

    result = chargeable_weight(gross_kg=volumetric, dimensions=REFERENCE, kg_per_cbm=167.0)

    assert result.basis is WeightBasis.GROSS
    assert result.chargeable_kg == pytest.approx(volumetric)


def test_the_chargeable_weight_is_never_below_either_input() -> None:
    for gross in (1.0, 13.0, 13.4, 500.0, 5000.0):
        result = chargeable_weight(gross_kg=gross, dimensions=REFERENCE, kg_per_cbm=167.0)

        assert result.chargeable_kg >= result.gross_kg
        assert result.chargeable_kg >= result.volumetric_kg


# --- refusals -------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0.0, -167.0])
def test_a_non_positive_ratio_is_refused(bad: float) -> None:
    with pytest.raises(ValueError, match="kg_per_cbm"):
        volumetric_weight_kg(REFERENCE, kg_per_cbm=bad)


def test_a_non_positive_gross_weight_is_refused() -> None:
    with pytest.raises(ValueError, match="gross_kg"):
        chargeable_weight(gross_kg=0.0, dimensions=REFERENCE, kg_per_cbm=167.0)


def test_nothing_here_reaches_the_network() -> None:
    """The formulas are published arithmetic. An external calculator service
    would add a dependency, break offline tests, and stop quotations during an
    outage — for a multiplication."""
    import inspect

    from translog_quote.domain.shipment import weight

    source = inspect.getsource(weight).lower()
    for forbidden in ("import requests", "import httpx", "import urllib", "import socket"):
        assert forbidden not in source
