"""The asynchronous rate-search job model.

One request, one deterministic identity, four explicit states:

    POST /api/rate-search -> QUEUED -> PROCESSING -> COMPLETED
                                                  -> FAILED

This module is pure shape. It imports no queue library, so the API service and
the browser worker share one vocabulary without either dragging in the other's
runtime — and so these types are testable with nothing running.

API-level job state is unrelated to WebCargo's own result-table pagination:
one names where a *job* is in its lifecycle, the other is a control inside the
provider's UI that the adapter must exhaust.
"""

from __future__ import annotations

import hashlib
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from translog_quote.domain.rates import FilterOutcome, LocationRef, RateQuery, Selection
from translog_quote.domain.shipment import CargoDimensions
from translog_quote.errors import ContractViolation


class JobState(StrEnum):
    """Every state a client can observe. Nothing in between is expressible."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


#: RQ's status vocabulary, translated exactly once. `deferred` and `scheduled`
#: are still "owed an answer, not started" from a client's point of view, and
#: everything RQ considers terminally unsuccessful is FAILED.
_RQ_STATUS_TO_STATE: dict[str, JobState] = {
    "queued": JobState.QUEUED,
    "deferred": JobState.QUEUED,
    "scheduled": JobState.QUEUED,
    "started": JobState.PROCESSING,
    "finished": JobState.COMPLETED,
    "failed": JobState.FAILED,
    "stopped": JobState.FAILED,
    "canceled": JobState.FAILED,
}


def job_state_from_rq_status(status: str) -> JobState:
    """One RQ status, as the state a client is shown.

    An unknown status is a contract violation rather than a guess: presenting
    a job as QUEUED because a new RQ version invented a status would tell a
    client to keep polling for a job that may never run.
    """
    state = _RQ_STATUS_TO_STATE.get(status)
    if state is None:
        raise ContractViolation(f"unknown RQ job status {status!r}")
    return state


class RateSearchJobRequest(BaseModel):
    """What a caller asks for. Validated at the API edge, replayed verbatim by
    the worker — the queue carries this shape and nothing looser.

    Origin and destination are the caller's own wording. No airport code is
    accepted here, because no caller-supplied code could name the mechanism
    that resolved it: WebCargo's own location lookup answers what a place is,
    inside the browser adapter, and records itself as the resolver.

    ``cargo_is_liquid`` mirrors the eligibility filter's contract: it is
    stated or unknown, never derived (AMB-3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    weight_kg: float = Field(gt=0)
    dimensions_in: CargoDimensions
    search_date: date

    commodity: str = Field(min_length=1)
    """Required, with no default, mirroring VR-5: commodity is a business
    fact the caller states. WebCargo's search form refuses to run without
    one, and injecting "General Cargo" on the caller's behalf would be
    business data nobody stated. The browser adapter selects the WebCargo
    commodity option that matches this wording exactly — or fails the job
    naming the mismatch, never guessing."""

    cargo_is_liquid: bool | None = None
    requires_door_delivery: bool = False

    def idempotency_key(self) -> str:
        """The job id this request deterministically owns.

        A byte-identical request maps to the same job, so a retried POST joins
        the queued work instead of queueing it twice. Field order is fixed by
        the model definition, which is what makes the serialisation — and
        therefore the digest — stable.
        """
        digest = hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()
        # A dash, not a colon: RQ permits only letters, numbers, underscores
        # and dashes in a job id (verified against a live queue).
        return f"rate-search-{digest}"

    def to_query(self) -> RateQuery:
        """The provider query, carrying stated places and no invented codes."""
        return RateQuery(
            origin=LocationRef(stated=self.origin),
            destination=LocationRef(stated=self.destination),
            weight_kg=self.weight_kg,
            dimensions_in=self.dimensions_in,
            date=self.search_date,
            commodity=self.commodity,
        )


class RateSearchJobResult(BaseModel):
    """Everything a completed search produced, including what it rejected and why.

    The same accountability the pipeline's `RateSearchOutcome` carries, in a
    shape that serialises: excluded rates keep their reasons, the selection
    keeps its generated explanation, and `is_simulated` travels with the data
    so no presentation layer can mistake simulated rates for provider rates.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    adapter_id: str
    is_simulated: bool
    returned: int
    query: RateQuery
    filtered: FilterOutcome
    selection: Selection | None

    completeness: str | None = None
    """The provider's own statement of the candidate set (e.g. "Showing the
    60 lowest rates"), carried so no consumer can present the selection as
    globally fastest beyond the candidates the provider actually returned."""


class JobStatus(BaseModel):
    """What `GET /api/rate-search/{job_id}` answers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    state: JobState
    result: RateSearchJobResult | None = None

    error: str | None = None
    """A failure's class name and message — never a traceback, never a page
    payload, never anything resembling a credential."""
