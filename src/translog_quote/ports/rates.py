"""The rate-provider boundary."""

from __future__ import annotations

from typing import Protocol

from translog_quote.domain.rates import RateQuery, RateSearchResult


class RateSearchPort(Protocol):
    """Rate search. Implemented by the mock adapter and, later, the real one.

    The port returns *normalised* rates. Raw payloads never cross this boundary as
    domain data, which is what makes selection unable to discover its own data
    source (AMB-1, consequence 5).

    An empty result is a valid outcome, not an error. Implementations filter
    nothing and rank nothing — both belong in `domain.rates`.
    """

    adapter_id: str

    def search(self, query: RateQuery) -> RateSearchResult: ...
