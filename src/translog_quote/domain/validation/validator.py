"""The validation engine.

Answers exactly one question: is this canonical shipment currently valid, and if
not, why? It never calls a model, never mutates the record, and never stops at
the first problem — a later phase batches every finding into one clarification
message (BR-9), which only works if every finding is collected in one pass.

No clarification text, no email composition, and no WebCargo or rate logic
belongs here — see ``domain.clarification`` for the former and ``domain.rates``
for the latter.
"""

from __future__ import annotations

from translog_quote.domain.shipment import DeliveryType, ShipmentRecord
from translog_quote.domain.validation.model import (
    FieldName,
    ValidationIssue,
    ValidationResult,
    ValidationRuleId,
    ValidationSeverity,
)

_MISSING = ValidationSeverity.MISSING
_INVALID = ValidationSeverity.INVALID


def _is_blank(value: str | None) -> bool:
    """Treat an empty or whitespace-only string as not stated.

    Mirrors ``domain.shipment.normalize``, so this rule holds even when a caller
    validates a record that was never passed through normalization — the
    validator is a pure function of its input and does not trust upstream
    behaviour it cannot see.
    """
    return value is None or not value.strip()


def _required(
    field: FieldName, rule_id: ValidationRuleId, message: str, *, is_missing: bool
) -> ValidationIssue | None:
    if not is_missing:
        return None
    return ValidationIssue(rule_id=rule_id, field=field, severity=_MISSING, message=message)


def _check_origin(record: ShipmentRecord) -> ValidationIssue | None:
    return _required(
        FieldName.ORIGIN,
        ValidationRuleId.ORIGIN_REQUIRED,
        "Origin is required.",
        is_missing=_is_blank(record.origin),
    )


def _check_destination(record: ShipmentRecord) -> ValidationIssue | None:
    return _required(
        FieldName.DESTINATION,
        ValidationRuleId.DESTINATION_REQUIRED,
        "Destination is required.",
        is_missing=_is_blank(record.destination),
    )


def _check_weight(record: ShipmentRecord) -> ValidationIssue | None:
    if record.weight_kg is None:
        return ValidationIssue(
            rule_id=ValidationRuleId.WEIGHT_REQUIRED,
            field=FieldName.WEIGHT_KG,
            severity=_MISSING,
            message="Weight (kg) is required.",
        )
    if record.weight_kg <= 0:
        return ValidationIssue(
            rule_id=ValidationRuleId.WEIGHT_INVALID,
            field=FieldName.WEIGHT_KG,
            severity=_INVALID,
            message=f"Weight must be a positive number of kilograms, got {record.weight_kg}.",
        )
    return None


def _check_dimensions(record: ShipmentRecord) -> ValidationIssue | None:
    # A non-positive dimension cannot reach this point: `CargoDimensions` enforces
    # `length, width, height > 0` at construction, so the only failure mode this
    # rule can observe is the whole value being absent.
    return _required(
        FieldName.DIMENSIONS_IN,
        ValidationRuleId.DIMENSIONS_REQUIRED,
        "Cargo dimensions are required.",
        is_missing=record.dimensions_in is None,
    )


def _check_commodity(record: ShipmentRecord) -> ValidationIssue | None:
    return _required(
        FieldName.COMMODITY,
        ValidationRuleId.COMMODITY_REQUIRED,
        "Commodity is required.",
        is_missing=_is_blank(record.commodity),
    )


def _check_cargo_type(record: ShipmentRecord) -> ValidationIssue | None:
    return _required(
        FieldName.CARGO_TYPE,
        ValidationRuleId.CARGO_TYPE_REQUIRED,
        "Cargo type is required.",
        is_missing=_is_blank(record.cargo_type),
    )


def _check_chemical_status(record: ShipmentRecord) -> ValidationIssue | None:
    return _required(
        FieldName.IS_CHEMICAL,
        ValidationRuleId.CHEMICAL_STATUS_REQUIRED,
        "Chemical status is required.",
        is_missing=record.is_chemical is None,
    )


def _check_pcs(record: ShipmentRecord) -> ValidationIssue | None:
    if record.pcs is None:
        return ValidationIssue(
            rule_id=ValidationRuleId.PCS_REQUIRED,
            field=FieldName.PCS,
            severity=_MISSING,
            message="Number of pieces is required.",
        )
    if record.pcs <= 0:
        return ValidationIssue(
            rule_id=ValidationRuleId.PCS_INVALID,
            field=FieldName.PCS,
            severity=_INVALID,
            message=f"Number of pieces must be a positive count, got {record.pcs}.",
        )
    return None


def _check_delivery_type(record: ShipmentRecord) -> ValidationIssue | None:
    return _required(
        FieldName.DELIVERY_TYPE,
        ValidationRuleId.DELIVERY_TYPE_REQUIRED,
        "Delivery type is required.",
        is_missing=record.delivery_type is None,
    )


def _check_msds_required_for_chemical(record: ShipmentRecord) -> ValidationIssue | None:
    """VR-8: conditional on ``is_chemical`` being exactly True.

    ``msds_attached`` is a yes/no answer, not a yes-only one — a shipment where
    the client has confirmed *no* MSDS (``msds_attached = False``) has answered
    the question and is not missing anything. Only ``None`` (never asked) is a
    finding.
    """
    if record.is_chemical is not True:
        return None
    return _required(
        FieldName.MSDS_ATTACHED,
        ValidationRuleId.MSDS_REQUIRED_FOR_CHEMICAL,
        "This shipment is chemical; MSDS status is required.",
        is_missing=record.msds_attached is None,
    )


def _check_address_required_for_door(record: ShipmentRecord) -> ValidationIssue | None:
    """VR-11: conditional on ``delivery_type`` being exactly DOOR."""
    if record.delivery_type is not DeliveryType.DOOR:
        return None
    return _required(
        FieldName.DELIVERY_ADDRESS,
        ValidationRuleId.ADDRESS_REQUIRED_FOR_DOOR,
        "Door delivery requires a delivery address.",
        is_missing=_is_blank(record.delivery_address),
    )


_RULES = (
    _check_origin,  # VR-1
    _check_destination,  # VR-2
    _check_weight,  # VR-3
    _check_dimensions,  # VR-4
    _check_commodity,  # VR-5
    _check_cargo_type,  # VR-6
    _check_chemical_status,  # VR-7
    _check_pcs,  # VR-9
    _check_delivery_type,  # VR-10
    _check_msds_required_for_chemical,  # VR-8, conditional
    _check_address_required_for_door,  # VR-11, conditional
)


def validate_shipment(record: ShipmentRecord) -> ValidationResult:
    """Run every rule against ``record`` and report every finding.

    Pure: no I/O, no model call, no mutation of ``record``. Calling this twice on
    an unchanged record always returns an equal result.
    """
    findings = [rule(record) for rule in _RULES]
    issues = tuple(issue for issue in findings if issue is not None)
    return ValidationResult(issues=issues)
