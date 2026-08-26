"""Validation vocabulary.

Extends the Phase 1 sketch (a bare list of missing fields) into a structured
result: every finding carries a stable rule identifier, a severity, which field
it concerns, and a human-readable explanation — so the result can drive a later
clarification message without any presentation code inspecting field names by
hand.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from translog_quote.domain.shipment.model import FieldName as FieldName  # re-export

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


class ValidationSeverity(StrEnum):
    """How serious a finding is.

    MISSING and INVALID are both produced by the eleven rules today. WARNING is
    declared for findings that do not block a quotation but are still worth
    surfacing — no current rule produces one, and none is invented here; the
    value exists so the type can carry a future rule without another redesign.
    """

    MISSING = "missing"
    INVALID = "invalid"
    WARNING = "warning"


class ValidationRuleId(StrEnum):
    """One stable identifier per rule (VR-1..VR-11 plus their INVALID variants).

    Machine-readable, and independent of the human-readable message — a later
    caller can group or filter on this without parsing text.
    """

    ORIGIN_REQUIRED = "ORIGIN_REQUIRED"  # VR-1
    DESTINATION_REQUIRED = "DESTINATION_REQUIRED"  # VR-2
    WEIGHT_REQUIRED = "WEIGHT_REQUIRED"  # VR-3
    WEIGHT_INVALID = "WEIGHT_INVALID"  # VR-3, present but not a positive number
    DIMENSIONS_REQUIRED = "DIMENSIONS_REQUIRED"  # VR-4
    COMMODITY_REQUIRED = "COMMODITY_REQUIRED"  # VR-5
    CARGO_TYPE_REQUIRED = "CARGO_TYPE_REQUIRED"  # VR-6
    CHEMICAL_STATUS_REQUIRED = "CHEMICAL_STATUS_REQUIRED"  # VR-7
    PCS_REQUIRED = "PCS_REQUIRED"  # VR-9
    PCS_INVALID = "PCS_INVALID"  # VR-9, present but not a positive count
    DELIVERY_TYPE_REQUIRED = "DELIVERY_TYPE_REQUIRED"  # VR-10
    MSDS_REQUIRED_FOR_CHEMICAL = "MSDS_REQUIRED_FOR_CHEMICAL"  # VR-8, conditional
    ADDRESS_REQUIRED_FOR_DOOR = "ADDRESS_REQUIRED_FOR_DOOR"  # VR-11, conditional


class ValidationIssue(BaseModel):
    """One finding from one rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: ValidationRuleId
    field: FieldName
    severity: ValidationSeverity
    message: str


class ValidationResult(BaseModel):
    """Every issue found in one pass. Never raised, never partial.

    An incomplete or invalid shipment is a normal business outcome, not an
    error — modelling it as an exception is the fastest route to a handler that
    also swallows a real integration fault (docs/architecture.md §12).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    issues: tuple[ValidationIssue, ...] = ()

    @property
    def missing_fields(self) -> tuple[FieldName, ...]:
        return tuple(
            issue.field for issue in self.issues if issue.severity is ValidationSeverity.MISSING
        )

    @property
    def invalid_fields(self) -> tuple[FieldName, ...]:
        return tuple(
            issue.field for issue in self.issues if issue.severity is ValidationSeverity.INVALID
        )

    @property
    def is_valid(self) -> bool:
        """Nothing blocks the shipment. A WARNING-only result is still valid —
        that is the entire distinction WARNING exists to make."""
        return not self.missing_fields and not self.invalid_fields

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity is ValidationSeverity.WARNING)
