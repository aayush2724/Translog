"""The canonical shipment record and its value objects."""

from translog_quote.domain.shipment.merge import (
    FieldConflict,
    MergeResult,
    build_initial_record,
    merge_shipment,
)
from translog_quote.domain.shipment.model import (
    CargoDimensions,
    DeliveryType,
    ExtractedFields,
    FieldName,
    RequestSource,
    ShipmentRecord,
)
from translog_quote.domain.shipment.normalize import normalize_extracted_fields

__all__ = [
    "CargoDimensions",
    "DeliveryType",
    "ExtractedFields",
    "FieldConflict",
    "FieldName",
    "MergeResult",
    "RequestSource",
    "ShipmentRecord",
    "build_initial_record",
    "merge_shipment",
    "normalize_extracted_fields",
]
