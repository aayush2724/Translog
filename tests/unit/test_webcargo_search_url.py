"""The search-surface URL is built correctly for a query-string base.

WebCargo's configured base carries a query string and the app routes on the
fragment; a naive "/"-join corrupts the query. These pin the join.
"""

from __future__ import annotations

import pytest

from translog_quote.adapters.webcargo.browser.pages import SEARCH_HASH, search_url


@pytest.mark.parametrize(
    "base",
    [
        "https://www.webcargonet.com/ajaxnew/?rand=408788&ctry=in",
        "https://www.webcargonet.com/ajaxnew/?rand=408788&ctry=in#inicio",
        "https://www.webcargonet.com/ajaxnew/?rand=408788&ctry=in#ebookings/dynamic-results",
    ],
)
def test_the_query_string_survives_and_the_fragment_is_the_search_hash(base: str) -> None:
    url = search_url(base)

    assert url.endswith(SEARCH_HASH)
    assert "ctry=in" in url
    assert "/#" not in url  # no separator was wedged before the fragment
    assert url.count("#") == 1  # any prior fragment was dropped, not stacked


def test_a_bare_base_gets_the_hash_appended() -> None:
    assert search_url("https://host/app") == "https://host/app" + SEARCH_HASH


# --- the volatile `rand` cache-buster is stripped ---------------------------------


def test_the_stale_rand_query_is_dropped_but_country_context_survives() -> None:
    """`rand` is a single-use nonce: re-navigating to a stale one bounces an
    authenticated session back to login, so it is stripped. Every other
    parameter (the country context) is kept verbatim."""
    url = search_url(
        "https://www.webcargonet.com/ajaxnew/?rand=408788&ctry=in#ebookings/search-and-book"
    )

    assert "rand=" not in url  # the stale nonce is gone
    assert "ctry=in" in url  # the country context stays
    assert url.endswith(SEARCH_HASH)
    assert url.count("#") == 1


def test_dropping_a_lone_rand_leaves_no_dangling_query() -> None:
    assert search_url("https://host/app?rand=1#x") == "https://host/app" + SEARCH_HASH


def test_rand_is_stripped_regardless_of_position_in_the_query() -> None:
    url = search_url("https://host/ajaxnew/?ctry=in&rand=99&lang=en")
    assert "rand=" not in url
    assert "ctry=in" in url
    assert "lang=en" in url
