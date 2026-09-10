"""The worker-side job function RQ executes, one search per invocation.

The domain boundary, kept: the provider supplies candidates and decides
nothing. `filter_rates` and `select_rate` run *here*, in the worker's
application layer, so eligibility and fastest-transit selection stay
deterministic domain code no matter which adapter produced the rates.

The provider is installed once by the worker entrypoint and reused for every
job — that is the persistent-session requirement travelling through the code:
`set_provider` is called at startup, never per job.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from translog_quote.domain.rates import FASTEST_ELIGIBLE, filter_rates, select_rate
from translog_quote.interface.jobs import RateSearchJobRequest, RateSearchJobResult

if TYPE_CHECKING:
    from translog_quote.ports import RateSearchPort

_provider: RateSearchPort | None = None


def set_provider(provider: RateSearchPort | None) -> None:
    """Install the provider every job on this worker uses.

    Called once by the worker entrypoint after it builds the (persistent)
    provider, and with ``None`` on shutdown. Jobs never build a browser.
    """
    global _provider  # noqa: PLW0603 - the worker process's one shared provider
    _provider = provider


def _resolve_provider() -> RateSearchPort:
    if _provider is not None:
        return _provider

    # A job executed outside our entrypoint (a bare `rq worker`, a test)
    # still gets the *configured* provider — and browser mode still routes
    # through the one builder allowed to construct it, which today refuses
    # until the extraction layer exists.
    from translog_quote import bootstrap
    from translog_quote.config import WebCargoMode

    settings = bootstrap.load_settings()
    if settings.webcargo.mode is WebCargoMode.BROWSER:
        return bootstrap.build_browser_rate_provider(settings)
    return bootstrap.build_rate_provider(settings)


def run_rate_search(payload: dict[str, Any]) -> dict[str, Any]:
    """One queued search: validate, search, filter, select, serialise.

    The return value is what RQ stores and what
    `GET /api/rate-search/{job_id}` hands back — the full accountability
    trail, not just a winner.
    """
    request = RateSearchJobRequest.model_validate(payload)
    provider = _resolve_provider()

    query = request.to_query()
    result = provider.search(query)

    filtered = filter_rates(
        result.rates,
        cargo_is_liquid=request.cargo_is_liquid,
        requires_door_delivery=request.requires_door_delivery,
    )
    selection = select_rate(filtered.eligible, FASTEST_ELIGIBLE)

    return RateSearchJobResult(
        adapter_id=result.adapter_id,
        is_simulated=result.is_simulated,
        returned=len(result.rates),
        query=query,
        filtered=filtered,
        selection=selection,
    ).model_dump(mode="json")
