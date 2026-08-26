"""Validation vocabulary.

The eleven rules themselves (VR-1..VR-11) are implemented in Phase 2, each as a
separately named predicate so a failing test points at one rule rather than at a
compound boolean.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class FieldName(StrEnum):
    """Every field the checklist can report as missing."""

    ORIGIN = "origin"
    DESTINATION = "destination"
    WEIGHT_KG = "weight_kg"
    DIMENSIONS_IN = "dimensions_in"
    COMMODITY = "commodity"
    CARGO_TYPE = "cargo_type"
    IS_CHEMICAL = "is_chemical"
    MSDS_ATTACHED = "msds_attached"
    PCS = "pcs"
    DELIVERY_TYPE = "delivery_type"
    DELIVERY_ADDRESS = "delivery_address"


REQUIRED_ALWAYS: frozenset[FieldName] = frozenset(
    {
        FieldName.ORIGIN,  # VR-1
        FieldName.DESTINATION,  # VR-2
        FieldName.WEIGHT_KG,  # VR-3
        FieldName.DIMENSIONS_IN,  # VR-4
        FieldName.COMMODITY,  # VR-5
        FieldName.CARGO_TYPE,  # VR-6
        FieldName.IS_CHEMICAL,  # VR-7
        FieldName.PCS,  # VR-9
        FieldName.DELIVERY_TYPE,  # VR-10
    }
)
"""The nine unconditional rules.

The two conditional rules are deliberately not expressible as a set, because each
depends on another field's value:

    VR-8   msds_attached     required only when is_chemical is True
    VR-11  delivery_address  required only when delivery_type is DOOR
"""


class ValidationResult(BaseModel):
    """Complete, or a list of what is missing.

    An incomplete shipment is a normal business outcome, not an error — modelling
    it as an exception is the fastest route to a handler that also swallows a real
    integration fault (docs/architecture.md §12).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    missing: tuple[FieldName, ...] = ()

    @property
    def is_complete(self) -> bool:
        return not self.missing
