"""Rate search: a validated shipment in, one selected rate out.

    ShipmentRecord -> RateQuery -> RateSearchPort -> Rate[]
                   -> filter -> select -> Selection

Four separate stages, deliberately never one function. Mapping changes shape but
never membership; filtering changes membership but never order; selection
changes order but never membership. Each is testable for the property it is
supposed to preserve.

The state machine's own edges are used unchanged: VALIDATED -> RATE_SELECTED
when something survives, VALIDATED -> NO_ELIGIBLE_RATE when nothing does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from translog_quote.domain.rates import (
    FASTEST_ELIGIBLE,
    RateQuery,
    filter_rates,
    select_rate,
)
from translog_quote.domain.routing import resolve_iata
from translog_quote.domain.workflow import RequestState
from translog_quote.errors import ContractViolation
from translog_quote.pipeline.audit import AuditEvent, AuditEventType
from translog_quote.pipeline.state_machine import StateMachine

if TYPE_CHECKING:
    import datetime

    from translog_quote.domain.rates import FilterOutcome, Selection, SelectionStrategy
    from translog_quote.domain.shipment import ShipmentRecord
    from translog_quote.pipeline.audit import AuditSink
    from translog_quote.ports import RateSearchPort


@dataclass(frozen=True, slots=True)
class RateSearchOutcome:
    """Everything the search produced, including what it rejected and why."""

    request_id: str
    state: RequestState
    query: RateQuery
    adapter_id: str
    returned: int
    filtered: FilterOutcome
    selection: Selection | None
    is_simulated: bool = True

    @property
    def has_selection(self) -> bool:
        return self.selection is not None

    @property
    def uses_mock_data(self) -> bool:
        """Whether these rates were invented rather than obtained from a provider.

        Surfaced so a demo or a review view can say so plainly. Nothing in the
        domain reads it — selection cannot see provenance at all.

        Reads the adapter's own declaration rather than inspecting its name. It
        previously tested ``adapter_id.startswith("mock")``, which quietly
        treated *anything* not named "mock" as real data — so a second
        simulated adapter would have been presented as a provider's rates.
        """
        return self.is_simulated


def build_query(record: ShipmentRecord, *, on_date: datetime.date) -> RateQuery:
    """Turn a validated shipment into a provider query.

    ``on_date`` is a required argument with no default. The date a rate search is
    run for is a business decision with no approved source in this project
    (AMB-8), so nothing here invents one — not today, not tomorrow, not the next
    available flight. The caller states it, and is accountable for it.
    """
    if record.weight_kg is None or record.weight_kg <= 0:
        raise ContractViolation("cannot search rates without a positive weight")
    if record.dimensions_in is None:
        raise ContractViolation("cannot search rates without dimensions")

    return RateQuery(
        origin_iata=resolve_iata(record.origin),
        destination_iata=resolve_iata(record.destination),
        weight_kg=record.weight_kg,
        dimensions_in=record.dimensions_in,
        date=on_date,
    )


class RateSearchStage:
    """Runs the four rate-pipeline stages for one validated shipment."""

    def __init__(
        self,
        *,
        provider: RateSearchPort,
        strategy: SelectionStrategy = FASTEST_ELIGIBLE,
        audit: AuditSink | None = None,
        clock: object | None = None,
    ) -> None:
        self._provider = provider
        self._strategy = strategy
        self._audit = audit
        self._clock = clock
        self._machine = StateMachine()

    def run(
        self,
        request_id: str,
        record: ShipmentRecord,
        *,
        on_date: datetime.date,
        cargo_is_liquid: bool | None = None,
    ) -> RateSearchOutcome:
        """Search, filter, select.

        ``cargo_is_liquid`` is passed through to the eligibility filter and is
        never derived here. The canonical record cannot express physical form,
        and guessing it from a commodity name would put a wrong carrier on a
        real quotation (AMB-3).
        """
        query = build_query(record, on_date=on_date)

        result = self._provider.search(query)
        self._emit(
            request_id,
            AuditEventType.RATES_FETCHED,
            {"adapter": result.adapter_id, "returned": len(result.rates)},
        )
        self._emit(
            request_id,
            AuditEventType.RATES_NORMALIZED,
            {"count": len(result.rates)},
        )

        filtered = filter_rates(result.rates, cargo_is_liquid=cargo_is_liquid)
        self._emit(
            request_id,
            AuditEventType.RATES_FILTERED,
            {
                "eligible": len(filtered.eligible),
                "excluded": [
                    {"carrier": e.rate.carrier_code, "reason": e.reason.value}
                    for e in filtered.excluded
                ],
            },
        )

        selection = select_rate(filtered.eligible, self._strategy)

        target = RequestState.RATE_SELECTED if selection else RequestState.NO_ELIGIBLE_RATE
        self._machine.assert_transition(RequestState.VALIDATED, target)
        self._emit(
            request_id,
            AuditEventType.STATE_CHANGED,
            {"from": RequestState.VALIDATED.value, "to": target.value},
        )
        if selection is not None:
            self._emit(
                request_id,
                AuditEventType.RATE_SELECTED,
                {
                    "carrier": selection.rate.carrier_code,
                    "transit_hours": selection.rate.transit.hours
                    if selection.rate.transit
                    else None,
                    "reason": selection.reason,
                },
            )

        return RateSearchOutcome(
            request_id=request_id,
            state=target,
            query=query,
            adapter_id=result.adapter_id,
            returned=len(result.rates),
            filtered=filtered,
            selection=selection,
            is_simulated=result.is_simulated,
        )

    def _emit(self, request_id: str, event: AuditEventType, detail: dict[str, object]) -> None:
        """Carrier codes, counts and reasons. Never a credential, never a payload."""
        if self._audit is None or self._clock is None:
            return
        now = self._clock.now()  # type: ignore[attr-defined]
        self._audit.record(AuditEvent(request_id=request_id, event=event, at=now, detail=detail))
