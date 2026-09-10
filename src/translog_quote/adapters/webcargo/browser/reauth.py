"""Operator re-authentication: a person signs in; this code only opens the door.

Runs the SAME persistent profile the worker uses, headed instead of headless,
so whatever the operator completes — password, MFA, CAPTCHA, anything the
provider asks of a human — lands in the profile directory and the restarted
worker inherits an authenticated session.

Nothing here reads, fills, submits, or bypasses any login control, by design
and by review: the only page action taken is navigating to the *configured*
WebCargo URL when one is set. The mirror of `gmail_auth.run_consent_flow` —
authentication is always an explicit human ceremony, never a side effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.adapters.webcargo.browser.driver import launch_persistent_chromium

if TYPE_CHECKING:
    from collections.abc import Callable

    from translog_quote.config import Settings


def run_operator_login(settings: Settings, *, prompt: Callable[[str], str] = input) -> None:
    """Open the profile headed, let the person work, save by closing cleanly.

    ``prompt`` is injectable so the flow is testable without a terminal.
    """
    handle = launch_persistent_chromium(settings, headless=False)
    try:
        page = handle.new_page()
        if settings.webcargo.base_url:
            page.goto(settings.webcargo.base_url)
        prompt(
            "A browser window is open on the persistent WebCargo profile.\n"
            "Sign in there yourself (complete any MFA/CAPTCHA as normal).\n"
            "When you are signed in and can see the rate-search UI, press "
            "Enter here to save the session and close the browser... "
        )
    finally:
        handle.close()
