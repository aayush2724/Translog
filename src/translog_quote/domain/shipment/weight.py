"""Chargeable weight — what air freight is actually priced on.

    gross weight        what the shipment weighs
    volumetric weight   what the space it occupies is worth
    chargeable weight   the greater of the two

A carrier bills whichever is larger, because a hold runs out of space and
payload at different rates. Quoting on gross weight alone under-prices light,
bulky cargo — and the error is silent, because a dense shipment gives the same
answer either way. The reference shipment (500 kg in 34 x 24 x 6 inches) is
dense: gross wins, and nothing looks wrong.

**This module computes; it decides nothing.** ``kg_per_cbm`` — the volumetric
ratio, also called the dimensional factor — is a required argument with no
default. It is a commercial term, not a constant: it varies by mode, by lane
and by carrier agreement, and Freightos models it as per-rate data
(``FreightPriceIndicatorType.volumetricRatio``) rather than as a fixed number.
The widely used IATA air figure is 167 kg/cbm (equivalently 6000 cm3/kg), but
whether *this* company's carrier agreements use it is a question for Translog,
not an assumption to bury in a default.

Nothing here is derived from a third-party service. The arithmetic is published
and unambiguous, so calling out to one would add a network dependency, break
offline tests, and stop quotations during an outage — for a multiplication.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from translog_quote.domain.shipment.model import CargoDimensions

#: Cubic metres in one cubic inch. A unit conversion, exact by definition
#: (1 inch = 2.54 cm), not a business rule: 0.0254^3.
CBM_PER_CUBIC_INCH = 0.0254**3

#: The IATA air-cargo dimensional factor in common use. Provided so a caller
#: can *choose* it explicitly and readably. Deliberately not a default
#: anywhere: see the module docstring.
IATA_AIR_KG_PER_CBM = 167.0


class WeightBasis(StrEnum):
    """Which weight the charge is based on.

    Worth reporting rather than inferring downstream: "charged on volumetric
    weight" is the sentence that explains an otherwise surprising price to a
    client, and it is the first thing a quotation maker checks.
    """

    GROSS = "gross"
    VOLUMETRIC = "volumetric"


class ChargeableWeight(BaseModel):
    """The chargeable weight, and both inputs that produced it.

    Both are kept because the comparison is the explanation. A result carrying
    only the winning number cannot say why it won.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    gross_kg: float = Field(gt=0)
    volumetric_kg: float = Field(gt=0)
    chargeable_kg: float = Field(gt=0)
    basis: WeightBasis
    kg_per_cbm: float = Field(gt=0)
    """The ratio this result was computed with. Recorded so a quotation can be
    reproduced later, when the agreement in force may have changed."""

    @property
    def volumetric_governs(self) -> bool:
        return self.basis is WeightBasis.VOLUMETRIC


def volume_cbm(dimensions: CargoDimensions) -> float:
    """Shipment volume in cubic metres. Pure geometry."""
    cubic_inches = dimensions.length * dimensions.width * dimensions.height
    return cubic_inches * CBM_PER_CUBIC_INCH


def volumetric_weight_kg(dimensions: CargoDimensions, *, kg_per_cbm: float) -> float:
    """What the occupied space weighs, at the given dimensional factor."""
    if kg_per_cbm <= 0:
        raise ValueError("kg_per_cbm must be positive")
    return volume_cbm(dimensions) * kg_per_cbm


def chargeable_weight(
    *, gross_kg: float, dimensions: CargoDimensions, kg_per_cbm: float
) -> ChargeableWeight:
    """The greater of gross and volumetric weight, with both inputs retained.

    Ties resolve to ``GROSS``: when the two are equal there is no volumetric
    uplift to report, and saying "charged on volumetric weight" would imply one.
    """
    if gross_kg <= 0:
        raise ValueError("gross_kg must be positive")

    volumetric = volumetric_weight_kg(dimensions, kg_per_cbm=kg_per_cbm)
    governs_volumetric = volumetric > gross_kg

    return ChargeableWeight(
        gross_kg=gross_kg,
        volumetric_kg=volumetric,
        chargeable_kg=volumetric if governs_volumetric else gross_kg,
        basis=WeightBasis.VOLUMETRIC if governs_volumetric else WeightBasis.GROSS,
        kg_per_cbm=kg_per_cbm,
    )
