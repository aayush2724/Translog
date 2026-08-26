"""The extraction contract — what a language model is allowed to tell us.

An extraction result is a record of *what the email said*, field by field, with
the reason a field has no value kept alongside it. It is deliberately richer
than ``ExtractedFields``: the canonical record can only say "known" or "null",
and that single null would otherwise collapse four different situations into
one — the email was silent, the client explicitly said no, the email said
something we cannot represent, or the model could not tell.

Nothing here validates a shipment, and nothing here decides anything. Whether a
missing MSDS matters is `domain.validation`'s question, and it is asked after
this type has already been produced.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from translog_quote.domain.shipment import CargoDimensions, DeliveryType


class FieldStatus(StrEnum):
    """Why a field holds the value it holds — or holds nothing.

    The three non-``STATED`` cases all map to ``None`` in the canonical record.
    They are kept apart here because they mean different things to a human
    reading an audit trail, and because a later phase composing a clarification
    should not ask again about something the client already answered.
    """

    STATED = "stated"
    """The email states this value. ``value`` is set."""

    NOT_STATED = "not_stated"
    """The email is silent about this field. Not an error, and not a denial."""

    DENIED = "denied"
    """The client explicitly stated that this field has no value.

    Distinct from ``NOT_STATED``: "we have no MSDS for this cargo" is an answer,
    whereas an email that never mentions an MSDS is not. For boolean fields the
    answer is usually better carried as ``STATED`` with ``value=False`` — see
    the guidance on `ExtractionResult.msds_attached`.
    """

    AMBIGUOUS = "ambiguous"
    """The email says something about this field that cannot be represented.

    A weight in pounds, dimensions in centimetres, two contradicting values in
    one message. The canonical model has exactly one unit per measurement and no
    conversion rule is defined, so the honest answer is to record what was seen
    and decline to guess. ``note`` explains what was found.
    """


class ExtractedValue[T](BaseModel):
    """One field's worth of extraction, and why it says what it says.

    ``evidence`` is the span of email text the value came from. It exists so a
    reviewer can check an extraction without re-reading the whole email, and so
    a wrong extraction can be traced to the sentence that caused it. It is never
    used for control flow.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: FieldStatus
    value: T | None = None
    evidence: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _status_and_value_agree(self) -> ExtractedValue[T]:
        if self.status is FieldStatus.STATED:
            if self.value is None:
                raise ValueError("status=STATED requires a value")
        elif self.value is not None:
            raise ValueError(f"status={self.status.value} must not carry a value")

        if self.status is FieldStatus.AMBIGUOUS and not self.note:
            raise ValueError("status=AMBIGUOUS requires a note explaining the ambiguity")

        return self

    # Constructors. Named rather than positional because at a call site
    # `ExtractedValue.not_stated()` reads as a decision and `ExtractedValue(...)`
    # reads as bookkeeping.

    @classmethod
    def stated(cls, value: T, *, evidence: str | None = None) -> ExtractedValue[T]:
        return cls(status=FieldStatus.STATED, value=value, evidence=evidence)

    @classmethod
    def not_stated(cls) -> ExtractedValue[T]:
        return cls(status=FieldStatus.NOT_STATED)

    @classmethod
    def denied(cls, *, evidence: str | None = None, note: str | None = None) -> ExtractedValue[T]:
        return cls(status=FieldStatus.DENIED, evidence=evidence, note=note)

    @classmethod
    def ambiguous(cls, *, note: str, evidence: str | None = None) -> ExtractedValue[T]:
        return cls(status=FieldStatus.AMBIGUOUS, note=note, evidence=evidence)

    @property
    def is_stated(self) -> bool:
        return self.status is FieldStatus.STATED


class ExtractionResult(BaseModel):
    """One email, extracted. Never more than one email.

    Cross-message reasoning does not happen here. Extracting the reply "actually,
    the cargo is 700 kg" yields ``weight_kg = 700`` and nothing else — it does
    not know 500 was said earlier, and it does not try to reconcile them.
    Comparing the two is `domain.shipment.merge_shipment`'s job, and disagreement
    there is a recorded conflict, not an extraction problem.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: ExtractedValue[str] = ExtractedValue[str].not_stated()
    destination: ExtractedValue[str] = ExtractedValue[str].not_stated()
    weight_kg: ExtractedValue[float] = ExtractedValue[float].not_stated()
    dimensions_in: ExtractedValue[CargoDimensions] = ExtractedValue[CargoDimensions].not_stated()
    commodity: ExtractedValue[str] = ExtractedValue[str].not_stated()
    cargo_type: ExtractedValue[str] = ExtractedValue[str].not_stated()
    is_chemical: ExtractedValue[bool] = ExtractedValue[bool].not_stated()
    msds_attached: ExtractedValue[bool] = ExtractedValue[bool].not_stated()
    """Whether an MSDS is attached to *this* email.

    "MSDS attached" is ``STATED True``. "No MSDS available" is ``STATED False`` —
    an explicit answer, not an absence, which is why it is not ``DENIED``. "MSDS
    to follow" is ``STATED False`` with the promise recorded in ``note``: no
    MSDS is attached now, which is the only thing this field claims.
    """

    pcs: ExtractedValue[int] = ExtractedValue[int].not_stated()
    delivery_type: ExtractedValue[DeliveryType] = ExtractedValue[DeliveryType].not_stated()
    delivery_address: ExtractedValue[str] = ExtractedValue[str].not_stated()

    @model_validator(mode="after")
    def _stated_numbers_are_possible(self) -> ExtractionResult:
        """Reject impossible numbers at the boundary rather than downstream.

        A client email does not say "-500 kg"; a model that produces one has
        malfunctioned, and letting it through would surface later as a confusing
        validation failure about the *shipment* rather than a clear failure of
        the *extraction*. Dimensions need no check here — ``CargoDimensions``
        already refuses non-positive sides at construction.
        """
        if self.weight_kg.is_stated:
            weight = self.weight_kg.value
            if weight is not None and weight <= 0:
                raise ValueError(f"extracted weight_kg must be positive, got {weight}")

        if self.pcs.is_stated:
            pieces = self.pcs.value
            if pieces is not None and pieces <= 0:
                raise ValueError(f"extracted pcs must be positive, got {pieces}")

        return self

    def fields_by_status(self, status: FieldStatus) -> tuple[str, ...]:
        """Field names currently carrying ``status``, for audit and diagnostics."""
        return tuple(
            name for name in type(self).model_fields if getattr(self, name).status is status
        )
