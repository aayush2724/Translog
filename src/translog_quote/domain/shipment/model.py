"""The canonical shipment record.

Shape taken verbatim from the project specification (docs/reference/). No fields
are added. Both the email path and any future portal path produce this one shape,
so every step downstream deals with a single format.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RequestSource(StrEnum):
    """How the request reached us."""

    EMAIL = "email"
    PORTAL = "portal"  # Specification step 1a. Out of scope for the demo.


class DeliveryType(StrEnum):
    DOOR = "door"
    AIRPORT = "airport"


class CargoDimensions(BaseModel):
    """Dimensions in inches.

    The reference thread wrote these as "24 (width) x 34 (length) x 6 (breadth)",
    which the specification maps to length=34, width=24, height=6. Axis labels are
    read from the text rather than assumed positionally (AMB-15).
    """

    model_config = ConfigDict(frozen=True)

    length: float = Field(gt=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class FieldName(StrEnum):
    """Every shipment field a validation issue, merge change, or conflict can
    concern. Lives here, rather than in ``domain.validation``, because it names
    ``ShipmentRecord``'s own fields — validation and merging both consume this
    vocabulary, so it belongs with the type it describes, not with either
    consumer.
    """

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


class ExtractedFields(BaseModel):
    """What the model returned for one email.

    Every field is present, and is either a value or an explicit ``None``. There is
    no third "absent" state — that distinction is what makes BR-7 (never guess a
    default) testable rather than a matter of trust.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: str | None = None
    destination: str | None = None
    weight_kg: float | None = None
    dimensions_in: CargoDimensions | None = None
    commodity: str | None = None
    cargo_type: str | None = None
    is_chemical: bool | None = None
    msds_attached: bool | None = None
    pcs: int | None = None
    delivery_type: DeliveryType | None = None
    delivery_address: str | None = None


class ShipmentRecord(BaseModel):
    """The canonical record. One fixed shape for the whole pipeline.

    ``cargo_type`` and ``is_chemical`` are independent (BR-12). In the reference
    thread the client wrote "Cargo type Non Haz" and, two days later, "This is a
    chemical product" — both true. Nothing in this type permits deriving one from
    the other, because doing so would silently skip the MSDS requirement (VR-8).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    source: RequestSource

    origin: str | None = None
    destination: str | None = None
    weight_kg: float | None = None
    dimensions_in: CargoDimensions | None = None
    commodity: str | None = None
    cargo_type: str | None = None
    is_chemical: bool | None = None
    msds_attached: bool | None = None
    pcs: int | None = None
    delivery_type: DeliveryType | None = None
    delivery_address: str | None = None
