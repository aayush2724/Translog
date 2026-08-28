"""The normalised rate model and the pipeline's intermediate shapes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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


class RateQuery(BaseModel):
    """What we ask a rate provider for.

    There is no field capable of carrying a client identity. WebCargo has exactly
    one user, Translog, and no client credential or identity ever reaches it
    (BR-13) — that is enforced by this type's shape.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin_iata: str = Field(min_length=3, max_length=3)
    destination_iata: str = Field(min_length=3, max_length=3)
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
