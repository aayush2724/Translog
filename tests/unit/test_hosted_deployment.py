"""Running the existing server on a host that assigns the port and the name.

Two things a platform like Render changes about how this process is reached,
and nothing else:

- the port is handed over in ``PORT`` rather than chosen locally;
- the browser reaches the server by hostname, not by loopback address.

The second is the one with teeth. The cross-site guard was written for a
loopback-only server and refuses every state-changing POST whose ``Host`` is a
name — so on a deployed URL the page would load and then silently 403 on Check
mail, on approving a clarification and on approving a quotation. The allowlist
is what makes the deployed name legitimate, and these tests pin that it stays
an allowlist: loopback always, declared names as well, everything else refused.
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.unit.test_gmail_thread import ENQUIRY, ENQUIRY_EXTRACTION, ScriptedExtractor, StubSource

from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.config import Settings
from translog_quote.interface.web import __main__ as web_main
from translog_quote.interface.web.live_session import LiveSession
from translog_quote.interface.web.server import ALLOWED_HOSTS_VAR, DEFAULT_PORT, DemoServer

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def live_server() -> Iterator[DemoServer]:
    """A real server on an ephemeral port. Declared here rather than imported,
    so this module owns its own fixture instead of shadowing another's."""
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    settings = base.model_copy(
        update={
            "openrouter": base.openrouter.model_copy(update={"api_key": "test-not-a-credential"}),
            "demo": base.demo.model_copy(update={"state_dir": Path(tempfile.mkdtemp())}),
            "gmail": base.gmail.model_copy(
                update={
                    "test_address": "translog@example.com",
                    "sender_address": "translog@example.com",
                    "approver_address": "approvals@translog.example",
                    "send_enabled": True,
                }
            ),
        }
    )
    session = LiveSession(
        settings,
        source=StubSource(ENQUIRY),  # type: ignore[arg-type]
        sink=CollectingEmailSink(),
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION),  # type: ignore[arg-type]
    )
    instance = DemoServer(("127.0.0.1", 0), settings, live_session=session)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()


# --- the port the platform assigns ----------------------------------------------


def test_the_port_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "10000")
    seen: dict[str, object] = {}

    def fake_run(*, host: str, port: int, settings: object, live: bool) -> int:
        seen["host"], seen["port"] = host, port
        return 0

    monkeypatch.setattr(web_main, "run", fake_run)
    web_main.main([])

    assert seen["port"] == 10000


def test_an_explicit_port_flag_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "10000")
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        web_main,
        "run",
        lambda **kw: (seen.update(kw), 0)[1],  # type: ignore[arg-type,return-value]
    )

    web_main.main(["--port", "9999"])

    assert seen["port"] == 9999


def test_the_bind_address_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform routes to the container's external interface, not loopback."""
    monkeypatch.setenv("HOST", "0.0.0.0")  # noqa: S104 - the platform requires it
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        web_main,
        "run",
        lambda **kw: (seen.update(kw), 0)[1],  # type: ignore[arg-type,return-value]
    )

    web_main.main([])

    assert seen["host"] == "0.0.0.0"  # noqa: S104


def test_with_nothing_set_the_local_defaults_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of reading these as flag defaults rather than instead."""
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("HOST", raising=False)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        web_main,
        "run",
        lambda **kw: (seen.update(kw), 0)[1],  # type: ignore[arg-type,return-value]
    )

    web_main.main([])

    assert seen["port"] == DEFAULT_PORT
    assert seen["host"] == "127.0.0.1"


@pytest.mark.parametrize("bad", ["", "   ", "not-a-port", "80a"])
def test_an_unusable_port_falls_back_rather_than_crashing(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """A boot crash on a restarting host is a loop, not a failure anyone sees."""
    monkeypatch.setenv("PORT", bad)

    assert web_main._env_port() == DEFAULT_PORT


# --- the Host allowlist ----------------------------------------------------------


def post(server: DemoServer, host_header: str) -> int:
    """A state-changing POST carrying the Host a deployed browser would send."""
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request(
            "POST",
            "/api/live/poll",
            body=json.dumps({}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Host": host_header},
        )
        return connection.getresponse().status
    finally:
        connection.close()


def test_a_deployed_hostname_is_refused_until_it_is_declared(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blocker, stated as a test: without this the deployed UI is read-only."""
    monkeypatch.delenv(ALLOWED_HOSTS_VAR, raising=False)

    assert post(live_server, "translog.onrender.com") == 403


def test_a_declared_hostname_is_accepted(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOWED_HOSTS_VAR, "translog.onrender.com")

    assert post(live_server, "translog.onrender.com") == 200


def test_several_names_may_be_declared_at_once(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOWED_HOSTS_VAR, "one.example.com, two.example.com")

    assert post(live_server, "two.example.com") == 200


def test_an_undeclared_name_is_still_refused_while_others_are_declared(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An allowlist, not a switch that turns the guard off."""
    monkeypatch.setenv(ALLOWED_HOSTS_VAR, "translog.onrender.com")

    assert post(live_server, "attacker.example.com") == 403


def test_loopback_never_needs_declaring(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOWED_HOSTS_VAR, "translog.onrender.com")

    assert post(live_server, "127.0.0.1") == 200
    assert post(live_server, "localhost") == 200


def test_a_wildcard_is_not_a_supported_value(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`*` is a literal name here, so it cannot silently disable the check."""
    monkeypatch.setenv(ALLOWED_HOSTS_VAR, "*")

    assert post(live_server, "attacker.example.com") == 403
