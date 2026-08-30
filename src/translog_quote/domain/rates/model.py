"""The normalised rate model and the pipeline's intermediate shapes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from translog_quote.domain.shipment import CargoDimensions


class TransitUnit(StrEnum):
    HOURS = "hours"
    DAYS = "days"


class TransitTime(BaseModel):
    """Transit duration, carrying its unit.

    The unit is explicit because "2" meaning days and "2" meaning hours differ by a
    factor of twelve in the ranking, and a bare integer makes that a silent bug.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: int = Field(gt=0)
    unit: TransitUnit

    @property
    def hours(self) -> int:
        """Comparable magnitude. The single ordering key for BR-1."""
        return self.value if self.unit is TransitUnit.HOURS else self.value * 24


class RateRestrictions(BaseModel):
    """What a carrier will and will not take on this lane.

    AMB-3 is unresolved: the only documented restriction is that one carrier does
    not accept liquid products, and nothing maps a commodity to its physical form.
    The shape is deliberately minimal until the authoritative rule set is confirmed
    — fields are added when a rule needs them, not in anticipation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepts_liquids: bool | None = None

    serves_door_delivery: bool | None = None
    """Whether this rate includes door delivery, as the provider declares it.

    ``None`` means the provider did not say — and for a shipment that *requires*
    door delivery, "did not say" must exclude. The asymmetry with
    ``accepts_liquids`` is deliberate and inverted on purpose: an unknown liquid
    restriction risks losing an option, while an unknown delivery capability
    risks quoting a service nobody has agreed to perform. The first is a missed
    rate; the second is a commercial commitment made by silence.
    """


class Rate(BaseModel):
    """One normalised rate. Adapter-agnostic and provenance-free.

    Nothing on this type reveals whether it came from the mock or the real adapter.
    That is structural, not a convention: selection cannot branch on provenance
    because there is no field it could branch on (AMB-1, consequence 5).

    ``transit`` is nullable so that an unmapped field travels as data rather than as
    a crash. It can never reach selection — a rate without transit is *unrankable*
    under BR-1, and the DropUnrankableRate filter excludes it with a reason.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    carrier_code: str  # IATA, e.g. "EK"
    carrier_name: str
    product: str  # e.g. "GEN"
    total_amount: Decimal | None = None
    currency: str | None = None
    transit: TransitTime | None = None  # AMB-1
    restrictions: RateRestrictions = RateRestrictions()  # AMB-3
    source_ref: str = ""  # the adapter's opaque id — audit only


class LocationRef(BaseModel):
    """One end of a lane: what the client called it, and what it resolved to.

    The two are kept apart on purpose. ``stated`` is the client's own wording
    and is always present — it is what the workflow carries, displays and quotes
    against, so no enquiry can be turned away for naming a place the system has
    not heard of. ``code`` is a provider's identifier and is present only when
    something actually resolved it.

    ``resolved_by`` is what makes "never guess an airport" structural rather
    than a rule someone has to remember. A code cannot be set without naming the
    mechanism that produced it, and no mechanism in this repository derives one
    from a place name — so there is no path by which an inferred code reaches a
    rate query. A wrong code is the worst failure available to this system: the
    search succeeds, rates come back, and they price the wrong lane (AMB-9).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stated: str = Field(min_length=1)
    """Exactly what the client wrote. Never rewritten, never substituted."""

    code: str | None = None
    """A provider identifier — an IATA code, or whatever that provider uses."""

    resolved_by: str | None = None
    """The `resolver_id` that produced `code`. Required whenever `code` is set."""

    @model_validator(mode="after")
    def _a_code_must_name_its_source(self) -> LocationRef:
        if self.code is not None and not self.resolved_by:
            raise ValueError(
                "a resolved code must name the resolver that produced it; "
                "an unattributed code is indistinguishable from a guess"
            )
        if self.code is not None and not self.code.strip():
            raise ValueError("a resolved code may not be blank")
        return self

    @property
    def display(self) -> str:
        """What a person should read: the code when there is one, else the name."""
        return self.code or self.stated


class RateQuery(BaseModel):
    """What we ask a rate provider for.

    There is no field capable of carrying a client identity. WebCargo has exactly
    one user, Translog, and no client credential or identity ever reaches it
    (BR-13) — that is enforced by this type's shape.

    Origin and destination are `LocationRef`, not bare airport codes. The field
    pair they replace was `min_length=3, max_length=3`, which meant a query
    could not be built for a place nobody had already tabulated — so a lookup
    table had to exist, and every enquiry outside it was refused before any
    provider was asked. Carrying the stated place removes that gate without
    inventing anything to fill it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: LocationRef
    destination: LocationRef
    weight_kg: float = Field(gt=0)
    dimensions_in: CargoDimensions
    date: date  # AMB-8: source unconfirmed


class RateSearchResult(BaseModel):
    """What a rate provider returns.

    ``rates`` is already normalised: raw payloads never cross the port boundary as
    domain data. ``raw_payload`` is opaque and exists only so a run can be
    reconstructed from the audit trail — it is typed such that no domain module can
    read a field out of it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    rates: tuple[Rate, ...]
    adapter_id: str
    raw_payload: Any = None  # audit only — never read by domain logic

    is_simulated: bool = True
    """Whether these rates were invented rather than obtained from a provider.

    Defaults to ``True`` so that *not declaring* provenance means "assume
    simulated". Presenting invented rates as live is the expensive mistake;
    presenting live rates as simulated is merely conservative — so the safe
    value is the default, and only an adapter that actually called a provider
    sets this ``False``.

    Nothing in the domain reads it — selection still cannot see provenance
    (AMB-1, consequence 5). It exists so the *presentation* layer can disclose
    what a viewer is looking at.
    """


class ExclusionReason(StrEnum):
    """Why a rate did not survive filtering.

    Every exclusion carries one of these, because the quotation maker's review view
    has to show why a carrier is absent — silence there is indistinguishable from a
    bug.
    """

    INCOMPLETE_RATE = "incomplete_rate"  # BR-4: null total or currency
    UNRANKABLE_NO_TRANSIT = "unrankable_no_transit"  # consequence of AMB-1
    CARRIER_RESTRICTED = "carrier_restricted"  # BR-5, governed by AMB-3
    SERVICE_NOT_AVAILABLE = "service_not_available"  # the rate cannot perform the stated scope


class ExcludedRate(BaseModel):
    """A rate that was filtered out, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rate: Rate
    reason: ExclusionReason
    detail: str = ""


class FilterOutcome(BaseModel):
    """The result of the filter stage. Membership changed; order preserved."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    eligible: tuple[Rate, ...]
    excluded: tuple[ExcludedRate, ...] = ()
