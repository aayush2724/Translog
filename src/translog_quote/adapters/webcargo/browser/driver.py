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
session survives worker restarts **provided the directory itself survives**
and the profile is set to restore its session cookies at launch (see
`ensure_session_cookies_restored`, applied before every launch).
On an ephemeral filesystem it will not — deployments must mount persistent
storage for it, as documented on the setting.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from translog_quote.errors import PermanentFailure, WebCargoUnreachable

if TYPE_CHECKING:
    from translog_quote.config import Settings


class PlaywrightWebCargoDriver:
    """`pages.BrowserDriver`, implemented on a Playwright page.

    Selector-free on purpose: every selector belongs to `pages.py`, and this
    class only knows how to perform the protocol's operations on a real page.
    """

    def __init__(self, page: Any) -> None:
        self._page = page

    def goto(self, url: str) -> None:
        # A navigation failure — DNS, connection refused, timeout, a browser
        # net:: error — is WebCargo being *unreachable*, not a lost session or a
        # login page. Translate it so the worker's startup can back off and exit
        # non-78 (retry later) instead of writing needs_login. Playwright's
        # TimeoutError subclasses Error, so catching Error covers both.
        from playwright.sync_api import Error as PlaywrightError

        try:
            self._page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            raise WebCargoUnreachable(f"could not load WebCargo at {url}: {exc}") from exc

    def click(self, selector: str) -> None:
        self._page.click(selector)

    def fill(self, selector: str, text: str) -> None:
        self._page.fill(selector, text)

    def press(self, selector: str, key: str) -> None:
        self._page.press(selector, key)

    def wait_visible(self, selector: str, timeout_seconds: float) -> bool:
        try:
            self._page.wait_for_selector(
                selector, timeout=timeout_seconds * 1000, state="visible"
            )
        except Exception:  # noqa: BLE001 - "did not appear" is an answer, not a crash
            return False
        return True

    def wait_attached(self, selector: str, timeout_seconds: float) -> bool:
        """Whether ``selector`` is in the DOM (visible or not) within the bound.

        Like ``wait_visible``, a Playwright timeout is an answer ("no"), never a
        crash — and unlike ``evaluate`` the bound holds even when the page never
        yields an execution context (see ``pages.login_page_visible``)."""
        try:
            self._page.wait_for_selector(
                selector, timeout=timeout_seconds * 1000, state="attached"
            )
        except Exception:  # noqa: BLE001 - "did not appear" is an answer, not a crash
            return False
        return True

    def option_texts(self, selector: str) -> list[str]:
        texts = self._page.locator(selector).all_text_contents()
        return [str(text) for text in texts]

    def click_option(self, selector: str, text: str) -> None:
        self._page.locator(selector, has_text=text).first.click()

    def evaluate(self, script: str, argument: object = None) -> object:
        if argument is None:
            return self._page.evaluate(script)
        return self._page.evaluate(script, argument)

    def screenshot(self) -> str | None:
        """A best-effort PNG of the current page, for a diagnostic capture only.

        Optional by design — it is not on the ``BrowserDriver`` protocol, so the
        search flow reaches it duck-typed and the scripted test fakes need not
        implement it. Returns the written path, or ``None`` if the page cannot be
        captured: a diagnostic taken while the flow is already failing must never
        raise a second error over the first. The file lands in the OS temp dir
        under a recognisable prefix; cleaning it up is the operator's, not a
        rate search's, concern."""
        import os
        import tempfile

        try:
            handle, path = tempfile.mkstemp(prefix="webcargo-settle-", suffix=".png")
            os.close(handle)
            self._page.screenshot(path=path)
        except Exception:  # noqa: BLE001 - a failed diagnostic must not mask the real error
            return None
        return path


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


#: Chromium's "On startup" preference value for "Continue where you left off".
#: It is the only setting under which Chromium RESTORES session cookies — the
#: ones without an expiry — at a launch that follows a clean shutdown; without
#: it they are discarded, and only a crash-recovery start brings them back.
#: WebCargo's authentication cookie is a session cookie and the worker's warm
#: shutdown is always a clean exit, so every orderly restart lost the session
#: (Railway 2026-10-15, Fedora 2026-10-14) while restarts after crashes kept
#: it, which made the loss look intermittent. The site's `remember-me` cookie
#: survives but does not re-authenticate at the app URL, so there is no
#: fallback. Verified with scratch profiles: default launch and
#: `--restore-last-session` both drop the cookie; this preference keeps it.
RESTORE_ON_STARTUP_LAST_SESSION = 1

#: Where Chromium keeps the profile's preferences, relative to the user data dir.
PREFERENCES_RELATIVE_PATH = Path("Default") / "Preferences"


def ensure_session_cookies_restored(user_data_dir: Path) -> Path:
    """Set ``session.restore_on_startup`` to "continue where you left off" in
    the profile's Preferences, creating the file when the profile is new.

    Every other preference is preserved: the file is read, this one key is
    set, and the whole document is written back through a temporary file and
    an atomic replace, so an interrupted write can never leave Chromium a
    half-written file to reset. A Preferences file that is not a JSON object
    is refused rather than overwritten — overwriting would silently discard
    the rest of the profile's settings. Returns the Preferences path.
    """
    prefs = user_data_dir / PREFERENCES_RELATIVE_PATH
    data: dict[str, Any] = {}
    if prefs.exists():
        try:
            loaded = json.loads(prefs.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PermanentFailure(
                f"{prefs} is not readable JSON; refusing to overwrite the browser "
                f"profile's preferences: {exc}"
            ) from exc
        if not isinstance(loaded, dict):
            raise PermanentFailure(
                f"{prefs} does not hold a JSON object; refusing to overwrite it."
            )
        data = loaded
    session = data.get("session")
    if session is None:
        session = {}
        data["session"] = session
    elif not isinstance(session, dict):
        raise PermanentFailure(
            f"{prefs}: the 'session' preference is not an object; refusing to overwrite it."
        )
    if session.get("restore_on_startup") == RESTORE_ON_STARTUP_LAST_SESSION:
        return prefs
    session["restore_on_startup"] = RESTORE_ON_STARTUP_LAST_SESSION
    prefs.parent.mkdir(parents=True, exist_ok=True)
    tmp = prefs.with_name(prefs.name + ".translog-tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, prefs)
    return prefs


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
    # Before the launch, never after: Chromium reads the preference at startup
    # and decides then whether the saved session cookies come back.
    ensure_session_cookies_restored(webcargo.user_data_dir)

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
