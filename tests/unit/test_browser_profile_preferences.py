"""The persistent profile is told to restore its session cookies at launch.

WebCargo's authentication cookie is a session cookie. Chromium restores those
after a clean shutdown only when the profile's ``session.restore_on_startup``
is "continue where you left off"; the worker's warm shutdown is a clean exit,
so without the preference every orderly restart lost the session. These tests
pin the write (new profile, existing preferences preserved, idempotent,
corrupt file refused) and that the launch applies it BEFORE Chromium starts.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from translog_quote.adapters.webcargo.browser.driver import (
    PREFERENCES_RELATIVE_PATH,
    RESTORE_ON_STARTUP_LAST_SESSION,
    ensure_session_cookies_restored,
    launch_persistent_chromium,
)
from translog_quote.config.settings import Settings, WebCargoSettings
from translog_quote.errors import PermanentFailure


def _prefs(profile: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (profile / PREFERENCES_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    return data


def test_a_new_profile_gets_a_preferences_file_with_session_restore(tmp_path: Path) -> None:
    profile = tmp_path / "profile"

    written = ensure_session_cookies_restored(profile)

    assert written == profile / PREFERENCES_RELATIVE_PATH
    assert _prefs(profile)["session"]["restore_on_startup"] == RESTORE_ON_STARTUP_LAST_SESSION


def test_existing_preferences_are_preserved_around_the_one_key(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    prefs = profile / PREFERENCES_RELATIVE_PATH
    prefs.parent.mkdir(parents=True)
    prefs.write_text(
        json.dumps(
            {
                "profile": {"exit_type": "Normal", "name": "Person 1"},
                "session": {"restore_on_startup": 5, "startup_urls": ["https://example.test/"]},
                "browser": {"has_seen_welcome_page": True},
            }
        ),
        encoding="utf-8",
    )

    ensure_session_cookies_restored(profile)

    data = _prefs(profile)
    assert data["session"]["restore_on_startup"] == RESTORE_ON_STARTUP_LAST_SESSION
    assert data["session"]["startup_urls"] == ["https://example.test/"]
    assert data["profile"] == {"exit_type": "Normal", "name": "Person 1"}
    assert data["browser"] == {"has_seen_welcome_page": True}
    assert not list(prefs.parent.glob("*.translog-tmp")), "temporary file must be replaced away"


def test_an_already_correct_file_is_left_untouched(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    prefs = profile / PREFERENCES_RELATIVE_PATH
    prefs.parent.mkdir(parents=True)
    original = json.dumps({"session": {"restore_on_startup": 1}, "x": 1}, indent=3)
    prefs.write_text(original, encoding="utf-8")

    ensure_session_cookies_restored(profile)

    assert prefs.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("body", ["{ not json", json.dumps([1, 2]), json.dumps({"session": 7})])
def test_a_preferences_file_that_cannot_be_edited_safely_is_refused(
    tmp_path: Path, body: str
) -> None:
    profile = tmp_path / "profile"
    prefs = profile / PREFERENCES_RELATIVE_PATH
    prefs.parent.mkdir(parents=True)
    prefs.write_text(body, encoding="utf-8")

    with pytest.raises(PermanentFailure):
        ensure_session_cookies_restored(profile)

    assert prefs.read_text(encoding="utf-8") == body, "a refused file is never rewritten"


def test_launch_sets_the_preference_before_chromium_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preference must exist when Chromium is launched: it is read at startup."""
    profile = tmp_path / "profile"
    seen: dict[str, Any] = {}

    class _Context:
        def set_default_navigation_timeout(self, ms: int) -> None:
            seen["navigation_timeout_ms"] = ms

        def set_default_timeout(self, ms: int) -> None:
            seen["timeout_ms"] = ms

    class _Chromium:
        def launch_persistent_context(self, *, user_data_dir: str, headless: bool) -> _Context:
            prefs = Path(user_data_dir) / PREFERENCES_RELATIVE_PATH
            seen["preference_at_launch"] = (
                json.loads(prefs.read_text(encoding="utf-8"))["session"]["restore_on_startup"]
                if prefs.exists()
                else None
            )
            seen["user_data_dir"] = user_data_dir
            seen["headless"] = headless
            return _Context()

    class _Playwright:
        chromium = _Chromium()

        def stop(self) -> None:
            seen["stopped"] = True

    class _Starter:
        def start(self) -> _Playwright:
            return _Playwright()

    stub = types.ModuleType("playwright.sync_api")
    stub.sync_playwright = lambda: _Starter()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", stub)

    settings = Settings(webcargo=WebCargoSettings(user_data_dir=profile))
    handle = launch_persistent_chromium(settings)

    assert seen["preference_at_launch"] == RESTORE_ON_STARTUP_LAST_SESSION
    assert seen["user_data_dir"] == str(profile)
    assert seen["headless"] is False, "the worker runs a headed browser"
    assert handle is not None
