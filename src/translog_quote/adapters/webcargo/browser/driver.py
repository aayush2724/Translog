"""Launching the persistent Chromium session behind `ManagedBrowser`.

The one module that touches Playwright, and it touches only the lifecycle:
launch a persistent context on the configured profile directory, hand out
pages, shut down. No WebCargo URL, selector, or page behaviour lives here —
those belong to the pages/extraction layer, and are written only against
inspected UI evidence.

Playwright is an optional dependency (the `worker` extra) and is imported
lazily, so every other process — the API service, the demo, the tests —
imports this module without having it installed.

The profile directory (`webcargo.user_data_dir`) is what makes the session
persistent: cookies and storage live there, so the authenticated WebCargo
session survives worker restarts **provided the directory itself survives**.
On an ephemeral filesystem it will not — deployments must mount persistent
storage for it, as documented on the setting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from translog_quote.errors import PermanentFailure

if TYPE_CHECKING:
    from translog_quote.config import Settings


class PlaywrightHandle:
    """One persistent Chromium context, shaped as a `BrowserHandle`.

    Owns both the context and the Playwright driver behind it, so `close`
    tears down everything the launch created and a relaunch starts clean.
    """

    def __init__(self, playwright: Any, context: Any) -> None:
        self._playwright = playwright
        self._context = context

    def new_page(self) -> Any:
        return self._context.new_page()

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            self._playwright.stop()


def launch_persistent_chromium(settings: Settings, *, headless: bool | None = None) -> (
    PlaywrightHandle
):
    """Start (or reattach to) the persistent WebCargo browser profile.

    ``headless`` overrides the configured value for the one caller that needs
    a visible browser: the operator re-authentication command, where a person
    completes the login themselves.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PermanentFailure(
            "Playwright is not installed. The browser worker requires the "
            "'worker' extra: pip install '.[worker]' && playwright install chromium"
        ) from exc

    webcargo = settings.webcargo
    webcargo.user_data_dir.mkdir(parents=True, exist_ok=True)

    playwright = sync_playwright().start()
    try:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(webcargo.user_data_dir),
            headless=webcargo.headless if headless is None else headless,
        )
        context.set_default_navigation_timeout(webcargo.navigation_timeout_seconds * 1000)
        context.set_default_timeout(webcargo.navigation_timeout_seconds * 1000)
    except Exception:
        playwright.stop()
        raise

    return PlaywrightHandle(playwright, context)
