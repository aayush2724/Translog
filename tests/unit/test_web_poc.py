"""The Phase 9 web POC, driven offline.

The session runs the real pipeline — real merge, real validation, real
clarification wording, the real approval gate, the real mock adapter and
selection — behind the scripted scenario extractor. So these tests exercise
what the browser is told and what the buttons may do, not the pipeline itself,
which has its own suites.

The protections Phase 9 demands are asserted here by name: a clarification
cannot silently send, the approval boundary is real, mock rate data is
labelled, the quotation stays a preview, and no credential can reach the
frontend.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.config import Settings
from translog_quote.interface.web import serialize
from translog_quote.interface.web.server import _ACTIONS, _STATIC_FILES, DemoServer
from translog_quote.interface.web.session import (
    DemoSequenceError,
    DemoSession,
    DemoStep,
)

FAKE_KEY = "test-not-a-real-credential"

WEB_DIR = Path(__file__).resolve().parents[2] / "src" / "translog_quote" / "interface" / "web"
STATIC_DIR = WEB_DIR / "static"


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Fully explicit settings: mock rates, a fake key, no .env influence."""
    monkeypatch.setenv("TRANSLOG_OPENROUTER__API_KEY", FAKE_KEY)
    monkeypatch.setenv("TRANSLOG_WEBCARGO__MODE", "mock")
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def sink() -> CollectingEmailSink:
    return CollectingEmailSink()


@pytest.fixture
def session(settings: Settings, sink: CollectingEmailSink) -> DemoSession:
    return DemoSession(settings, sink=sink)


def snap(session: DemoSession) -> dict[str, object]:
    """The snapshot after a round trip through JSON, as the browser gets it."""
    payload = json.dumps(serialize.snapshot(session))
    result = json.loads(payload)
    assert isinstance(result, dict)
    return result


def complete(session: DemoSession) -> DemoSession:
    session.approve_clarification()
    session.receive_reply()
    session.search_rates()
    return session


# --- missing information displays correctly -------------------------------------


def test_the_dashboard_status_is_the_real_validators_verdict(session: DemoSession) -> None:
    state = snap(session)
    assert state["request_state"] == "needs_info"
    assert state["step"] == "enquiry_processed"


def test_the_four_missing_fields_are_listed_with_titles_and_questions(
    session: DemoSession,
) -> None:
    missing = snap(session)["missing"]
    assert isinstance(missing, list)
    assert [m["title"] for m in missing] == [
        "Commodity",
        "Chemical status",
        "Number of pieces",
        "Delivery type",
    ]
    assert all(m["question"] for m in missing)


def test_known_missing_and_conditional_fields_are_distinguished(session: DemoSession) -> None:
    shipment = snap(session)["shipment"]
    assert isinstance(shipment, list)
    fields = {f["field"]: f for f in shipment}

    assert fields["origin"]["status"] == "known"
    assert fields["origin"]["value"] == "Ahmedabad"
    assert fields["origin"]["evidence"] == "Origin: Ahmedabad"
    assert fields["commodity"]["status"] == "missing"
    assert fields["commodity"]["value"] is None
    # Conditional fields are not "missing" while their condition is unknown,
    # and are never displayed as if a value had been inferred.
    assert fields["msds_attached"]["status"] == "not_required"
    assert "chemical" in str(fields["msds_attached"]["note"]).lower()
    assert fields["delivery_address"]["status"] == "not_required"


# --- the clarification cannot silently send -------------------------------------


def test_a_drafted_clarification_is_not_sent(
    session: DemoSession, sink: CollectingEmailSink
) -> None:
    state = snap(session)
    clarification = state["clarification"]
    assert isinstance(clarification, dict)
    assert clarification["status"] == "draft"
    assert clarification["approved_by"] is None
    assert sink.sent == []


def test_the_reply_cannot_be_processed_before_a_person_approves(session: DemoSession) -> None:
    with pytest.raises(DemoSequenceError):
        session.receive_reply()
    with pytest.raises(DemoSequenceError):
        session.search_rates()
    with pytest.raises(DemoSequenceError):
        session.acknowledge_quotation()


def test_approval_is_the_only_path_that_releases_the_draft(
    session: DemoSession, sink: CollectingEmailSink
) -> None:
    session.approve_clarification()

    assert len(sink.sent) == 1  # the in-memory outbox; no sender exists
    state = snap(session)
    clarification = state["clarification"]
    assert isinstance(clarification, dict)
    assert clarification["status"] == "approved"
    assert "simulated" in str(clarification["approved_by"])
    assert state["request_state"] == "clarification_sent"


def test_approving_twice_is_refused(session: DemoSession) -> None:
    session.approve_clarification()
    with pytest.raises(DemoSequenceError):
        session.approve_clarification()


# --- merged shipment and validation state display correctly ---------------------


def test_the_reply_is_hidden_until_the_demo_reaches_it(session: DemoSession) -> None:
    """The browser cannot show a result before the action that produces it."""
    state = snap(session)
    assert state["reply"] is None
    assert state["merged"] is None
    assert state["rates"] is None
    assert state["quotation"] is None


def test_the_merged_shipment_carries_provenance(session: DemoSession) -> None:
    session.approve_clarification()
    session.receive_reply()

    merged = snap(session)["merged"]
    assert isinstance(merged, dict)
    fields = {f["field"]: f for f in merged["shipment"]}

    assert fields["origin"]["value"] == "Ahmedabad"
    assert fields["origin"]["source"] == "enquiry"
    assert fields["commodity"]["value"] == "Engineering components"
    assert fields["commodity"]["source"] == "reply"
    assert fields["delivery_type"]["value"] == "Airport to airport"

    assert "Origin" in merged["carried"]
    assert "Commodity" in merged["supplied"]
    assert merged["validation"]["is_valid"] is True
    assert merged["state"] == "validated"


# --- mock rate data is labelled --------------------------------------------------


def test_rates_are_labelled_as_mock_and_never_as_a_providers(session: DemoSession) -> None:
    complete(session)

    rates = snap(session)["rates"]
    assert isinstance(rates, dict)
    assert rates["uses_mock_data"] is True
    assert rates["adapter_id"] == "mock-webcargo"
    assert rates["returned"] == 6
    assert len(rates["eligible"]) == 4
    assert len(rates["excluded"]) == 2


def test_the_recommendation_is_the_backends_selection(session: DemoSession) -> None:
    """The frontend must not re-select; the snapshot carries the decision."""
    complete(session)

    rates = snap(session)["rates"]
    assert isinstance(rates, dict)
    selection = rates["selection"]
    assert selection["rate"]["carrier_name"] == "Turkish Cargo"
    assert selection["rate"]["transit"] == "1 day"
    assert "fastest eligible transit" in selection["reason"]

    recommended = [r for r in rates["eligible"] if r["recommended"]]
    assert [r["carrier_code"] for r in recommended] == ["TK"]

    # Ranked by transit, not price: Emirates (2 days, most expensive survivor)
    # outranks Etihad (4 days, cheaper).
    ranking = [r["carrier_name"] for r in selection["ranking"]]
    assert ranking.index("Emirates") < ranking.index("Etihad Airways")


# --- the quotation remains a preview ---------------------------------------------


def test_the_quotation_preview_is_flagged_and_invents_nothing(session: DemoSession) -> None:
    complete(session)

    quotation = snap(session)["quotation"]
    assert isinstance(quotation, dict)
    for flag in ("POC QUOTATION PREVIEW", "MOCK RATE DATA", "NOT SENT", "NOT APPROVED"):
        assert flag in quotation["flags"]
    rows = {row["label"]: row["value"] for row in quotation["shipment_rows"]}
    assert rows["Delivery address"] == "Not specified in POC"
    assert quotation["unspecified"] == ["Taxes and surcharges", "Validity", "Payment terms"]


def test_quotation_approval_is_simulated_and_sends_nothing(
    session: DemoSession, sink: CollectingEmailSink
) -> None:
    complete(session)
    session.acknowledge_quotation()

    state = snap(session)
    acknowledgement = state["quotation_acknowledgement"]
    assert isinstance(acknowledgement, dict)
    assert "nothing was sent" in str(acknowledgement["note"])
    # The request never claims QUOTATION_SENT, and the outbox still holds only
    # the one approved clarification — approval here dispatches nothing.
    assert state["request_state"] == "rate_selected"
    assert len(sink.sent) == 1
    assert session.step is DemoStep.QUOTATION_ACKNOWLEDGED


# --- no secrets reach the frontend -----------------------------------------------


def test_no_credential_or_auth_material_appears_in_any_snapshot(
    session: DemoSession,
) -> None:
    complete(session)
    session.acknowledge_quotation()

    payload = json.dumps(serialize.snapshot(session))
    for secret in (FAKE_KEY, "api_key", "Authorization", "Bearer", "password", "openrouter"):
        assert secret not in payload


def test_the_static_frontend_contains_no_unsafe_rendering_or_secrets() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    for source in (html, js):
        assert ".innerHTML" not in source
        assert "insertAdjacentHTML" not in source
        assert "document.write" not in source
        assert "eval(" not in source
        assert "localStorage" not in source
        for secret_marker in ("api_key", "sk-", "Bearer", "OPENROUTER"):
            assert secret_marker not in source
    # No inline handlers or inline script bodies: the CSP forbids them and the
    # markup must not depend on them.
    assert "onclick=" not in html
    assert '<script src="/app.js" defer></script>' in html


def test_the_static_surface_is_a_closed_whitelist() -> None:
    assert set(_STATIC_FILES) == {"/", "/index.html", "/app.css", "/app.js"}
    assert set(_ACTIONS) == {
        "approve-clarification",
        "receive-reply",
        "search-rates",
        "approve-quotation",
    }


# --- the HTTP server, end to end -------------------------------------------------


@pytest.fixture
def server(settings: Settings) -> Iterator[DemoServer]:
    instance = DemoServer(("127.0.0.1", 0), settings)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()


def request(server: DemoServer, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_the_server_serves_the_app_with_security_headers(server: DemoServer) -> None:
    status, headers, body = request(server, "GET", "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert b"Translog Express" in body


def test_the_server_refuses_paths_outside_the_whitelist(server: DemoServer) -> None:
    for path in ("/.env", "/../pyproject.toml", "/static/../../.env", "/app.js.map"):
        status, _, _ = request(server, "GET", path)
        assert status == 404, path


def test_actions_out_of_order_are_conflicts_not_progress(server: DemoServer) -> None:
    status, _, body = request(server, "POST", "/api/action/receive-reply")
    assert status == 409
    assert b"error" in body


def test_unknown_actions_do_not_exist(server: DemoServer) -> None:
    status, _, _ = request(server, "POST", "/api/action/send-quotation")
    assert status == 404


def test_the_whole_demo_flow_runs_over_http_and_resets(server: DemoServer) -> None:
    body = b""
    for action in ("approve-clarification", "receive-reply", "search-rates", "approve-quotation"):
        status, _, body = request(server, "POST", f"/api/action/{action}")
        assert status == 200, action
    state = json.loads(body)
    assert state["step"] == "quotation_acknowledged"

    status, _, body = request(server, "POST", "/api/action/reset")
    assert status == 200
    assert json.loads(body)["step"] == "enquiry_processed"
