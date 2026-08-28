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
from translog_quote.domain.shipment.weight import (
    IATA_AIR_KG_PER_CBM,
    ChargeableWeight,
    WeightBasis,
    chargeable_weight,
    volume_cbm,
    volumetric_weight_kg,
)

__all__ = [
    "IATA_AIR_KG_PER_CBM",
    "CargoDimensions",
    "ChargeableWeight",
    "DeliveryType",
    "ExtractedFields",
    "FieldConflict",
    "FieldName",
    "MergeResult",
    "RequestSource",
    "ShipmentRecord",
    "WeightBasis",
    "build_initial_record",
    "chargeable_weight",
    "merge_shipment",
    "normalize_extracted_fields",
    "volume_cbm",
    "volumetric_weight_kg",
]
