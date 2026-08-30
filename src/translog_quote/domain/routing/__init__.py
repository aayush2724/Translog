"""Place names as the client writes them.

Resolving a place to a provider's identifier is *not* done here and cannot be:
it needs a provider, and `domain` may not know one exists. That work sits
behind `ports.routing.LocationResolverPort`.

What remains is the pure part — tidying a name so two spellings of it compare
equal — and the rule that has always mattered most: a hallucinated airport code
is a silent wrong answer, because the search succeeds and returns rates for the
wrong lane (AMB-9). No module in this package produces a code, so none can
produce a wrong one.
"""

from translog_quote.domain.routing.places import is_nameable, normalise_place

__all__ = ["is_nameable", "normalise_place"]
