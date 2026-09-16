"""Worker startup authenticates the ONE live browser context, in-process.

Option 1: the operator signs in on the same persistent context the worker
serves from — there is no separate login process whose exit would drop the
in-memory session cookie. These pin the wiring:

- the bootstrap gate delegates to the browser adapter and no-ops otherwise;
- `--login` runs the worker with interactive auth, in the SAME process, and
  never prints a premature "session saved".

All fakes — no Playwright, no Redis, no WebCargo.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.unit.test_browser_manager import FakeHandle, Launcher
from tests.unit.test_webcargo_adapter import adapter_over
from tests.unit.test_webcargo_extraction import FakeDriver

from translog_quote import bootstrap
from translog_quote.adapters.webcargo.browser import SessionState


def test_the_gate_is_a_noop_for_a_provider_that_needs_no_browser() -> None:
    class Simple:
        adapter_id = "mock"

        def search(self, query: object) -> object:  # pragma: no cover - never called
            raise AssertionError

    # No adapter, no browser, no error: the gate simply does not apply.
    bootstrap.ensure_worker_session_authenticated(Simple(), interactive=True)  # type: ignore[arg-type]


def test_the_gate_authenticates_the_webcargo_adapter_on_its_live_context() -> None:
    handle = FakeHandle(1)
    launcher = Launcher(handle)
    adapter, _ = adapter_over(launcher, [FakeDriver(authenticated=True)])

    bootstrap.ensure_worker_session_authenticated(adapter, interactive=True, prompt=lambda _m: "")

    assert launcher.calls == 1
    assert not handle.closed  # context stays alive for the worker to serve on
    assert adapter._manager.state is SessionState.READY  # noqa: SLF001


def test_login_flag_runs_the_worker_with_interactive_auth_in_one_process(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from translog_quote.interface.worker import __main__ as entry
    from translog_quote.interface.worker import main as worker_main

    seen: dict[str, Any] = {}
    monkeypatch.setattr(bootstrap, "load_settings", lambda env_file=None: object())
    monkeypatch.setattr(
        worker_main,
        "run_worker",
        lambda settings, *, interactive_login=False: (
            seen.update(interactive=interactive_login),
            0,
        )[1],
    )

    assert entry.main(["--login"]) == 0
    assert seen["interactive"] is True
    # the old, misleading unconditional success message is gone:
    assert "Session saved" not in capsys.readouterr().out


def test_without_login_the_worker_expects_an_already_authenticated_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from translog_quote.interface.worker import __main__ as entry
    from translog_quote.interface.worker import main as worker_main

    seen: dict[str, Any] = {}
    monkeypatch.setattr(bootstrap, "load_settings", lambda env_file=None: object())
    monkeypatch.setattr(
        worker_main,
        "run_worker",
        lambda settings, *, interactive_login=False: (
            seen.update(interactive=interactive_login),
            0,
        )[1],
    )

    assert entry.main([]) == 0
    assert seen["interactive"] is False
