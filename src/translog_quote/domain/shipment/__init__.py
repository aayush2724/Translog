"""The canonical shipment record and its value objects."""

from translog_quote.domain.shipment.model import (
    CargoDimensions,
    DeliveryType,
    ExtractedFields,
    RequestSource,
    ShipmentRecord,
)

__all__ = [
    "CargoDimensions",
    "DeliveryType",
    "ExtractedFields",
    "RequestSource",
    "ShipmentRecord",
]
