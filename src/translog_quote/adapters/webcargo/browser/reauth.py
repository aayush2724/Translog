"""Operator re-authentication: a person signs in on the worker's LIVE session.

The worker owns one long-lived, persistent browser context for its whole
life. When it starts without an authenticated WebCargo session, an operator
signs in *in that same live context* — this module runs only the human
ceremony (a prompt) and then verifies the authenticated search form is
actually visible before the worker proceeds.

It never launches or closes a browser (either would drop the in-memory
session cookie that authenticates the app), never reads, fills, submits, or
bypasses any login control, and never exports a cookie. The mirror of the
Gmail consent flow: authentication is always an explicit human ceremony.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.adapters.webcargo.browser.pages import verify_authenticated

if TYPE_CHECKING:
    from collections.abc import Callable

    from translog_quote.adapters.webcargo.browser.pages import BrowserDriver


def run_operator_login(
    driver: BrowserDriver,
    *,
    base_url: str,
    navigation_timeout_seconds: float,
    prompt: Callable[[str], str] = input,
) -> None:
    """Prompt the operator to sign in on the live session, then verify.

    ``prompt`` is injectable so the ceremony is testable without a terminal.
    Raises ``WebCargoSessionLost`` (via ``verify_authenticated``) if the
    authenticated search form is not visible after the operator continues, so
    success is reported only when the search form is actually there — never a
    premature "signed in".
    """
    prompt(
        "A browser window is open on the persistent WebCargo profile.\n"
        "Sign in there yourself (complete any MFA/CAPTCHA as normal).\n"
        "When you can see the rate-search form, press Enter here to continue... "
    )
    verify_authenticated(driver, base_url=base_url, timeout_seconds=navigation_timeout_seconds)
    print(
        "Authenticated: the WebCargo rate-search form is visible; the worker "
        "will now serve jobs on this same browser session."
    )
