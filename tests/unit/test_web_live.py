"""The live web view over the real Gmail workflow, driven offline.

The mailbox, the model and the outbound sink are stubbed. The router,
correlation policy, clarification workflow, merge, validator, rate provider,
filter, selection, both gates and the state machine are all real — so what
these tests exercise is the actual browser-facing behaviour, not a rehearsal.

The protections this view must not weaken are asserted by name: a clarification
cannot send itself, the quotation gate cannot be reached without an explicit
named decision, a decline sends nothing, simulated rates are labelled wherever
they appear, and no credential can reach the frontend.
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.unit.test_gmail_thread import (
    ENQUIRY,
    ENQUIRY_EXTRACTION,
    REPLY,
    REPLY_EXTRACTION,
    ScriptedExtractor,
    StubSource,
)

from translog_quote import bootstrap
from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.store import JsonFileStore
from translog_quote.config import Settings
from translog_quote.domain.extraction import ExtractionResult
from translog_quote.domain.quotation import INTERNAL_SUBJECT_PREFIX, NotADecision
from translog_quote.domain.workflow import RequestState
from translog_quote.interface.web import live_serialize
from translog_quote.interface.web.live_serialize import SIMULATED_BANNER
from translog_quote.interface.web.live_session import LiveSequenceError, LiveSession
from translog_quote.interface.web.server import (
    _LIVE_ACTIONS,
    _LIVE_FILES,
    _STATIC_FILES,
    DemoServer,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

FAKE_KEY = "test-not-a-real-credential"
APPROVER = "ops.manager@translog.example"
APPROVER_MAILBOX = "approvals@translog.example"
CLIENT = "client@example.com"


@pytest.fixture
def settings() -> Settings:
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    return base.model_copy(
        update={
            "openrouter": base.openrouter.model_copy(update={"api_key": FAKE_KEY}),
            "demo": base.demo.model_copy(update={"state_dir": Path(tempfile.mkdtemp())}),
            "gmail": base.gmail.model_copy(
                update={
                    "test_address": "translog@example.com",
                    "sender_address": "translog@example.com",
                    "approver_address": APPROVER_MAILBOX,
                    "send_enabled": True,
                }
            ),
        }
    )


@pytest.fixture
def sink() -> CollectingEmailSink:
    return CollectingEmailSink()


def session_for(
    settings: Settings,
    sink: CollectingEmailSink,
    *,
    emails: tuple[object, ...] = (ENQUIRY,),
    extractions: tuple[object, ...] = (ENQUIRY_EXTRACTION,),
) -> LiveSession:
    return LiveSession(
        settings,
        source=StubSource(*emails),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(*extractions),  # type: ignore[arg-type]
    )


def to_client(sink: CollectingEmailSink) -> list[object]:
    return [m for m in sink.sent if m.to_address == CLIENT]


def to_approver(sink: CollectingEmailSink) -> list[object]:
    return [m for m in sink.sent if m.to_address == APPROVER_MAILBOX]


def only_request(session: LiveSession) -> object:
    return next(iter(session.requests.values()))


# --- polling: reads, never sends ------------------------------------------------


def test_polling_processes_the_enquiry_and_sends_nothing(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    session = session_for(settings, sink)

    session.poll()

    assert sink.sent == []
    request = only_request(session)
    assert request.state is RequestState.NEEDS_INFO  # type: ignore[attr-defined]
    assert request.awaiting_clarification_approval is True  # type: ignore[attr-defined]


def test_a_second_poll_reprocesses_nothing(settings: Settings, sink: CollectingEmailSink) -> None:
    """The extractor is scripted with one result, so a second call would raise."""
    session = session_for(settings, sink)
    session.poll()

    session.poll()

    assert session.last_poll_new == 0


def test_our_own_approval_mail_is_never_ingested(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    from translog_quote.domain.email import RawEmail

    internal = RawEmail(
        message_id="<review@translog.example>",
        from_address="translog@example.com",
        subject=f"{INTERNAL_SUBJECT_PREFIX} Quotation approval required — R-1",
        body_text="QUOTATION AWAITING APPROVAL",
        received_at=ENQUIRY.received_at,
    )
    session = session_for(settings, sink, emails=(ENQUIRY, internal))

    session.poll()

    assert session.skipped_internal == 1


# --- the clarification gate -----------------------------------------------------


def test_the_clarification_is_not_sent_until_a_person_approves(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    session = session_for(settings, sink)
    session.poll()

    assert sink.sent == []

    session.approve_clarification(by=APPROVER)

    assert len(to_client(sink)) == 1


def test_a_clarification_cannot_be_approved_anonymously(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    session = session_for(settings, sink)
    session.poll()

    with pytest.raises(LiveSequenceError, match="named person"):
        session.approve_clarification(by="   ")

    assert sink.sent == []


def test_approving_a_clarification_that_is_not_pending_is_refused(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    session = session_for(settings, sink)

    with pytest.raises(LiveSequenceError, match="No clarification draft"):
        session.approve_clarification(by=APPROVER)


def test_the_sent_clarification_state_is_persisted(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    session = session_for(settings, sink)
    session.poll()
    session.approve_clarification(by=APPROVER)

    stored = JsonFileStore(settings.demo.state_dir).get_request(only_request(session).request_id)  # type: ignore[attr-defined]
    assert stored is not None
    assert stored.state is RequestState.CLARIFICATION_SENT


# --- the reply, and automatic rate search ---------------------------------------


class GrowingSource:
    """A mailbox that gains a message between polls, as a real one does.

    Modelled rather than simulated by reaching into the session: the demo's
    whole point is that the client replies *later*, so the stub that stands in
    for Gmail is the right place for that to be true.
    """

    def __init__(self, *states: tuple[object, ...]) -> None:
        self._states = list(states)
        self._last: tuple[object, ...] = ()

    def fetch_new(self) -> tuple[object, ...]:
        if self._states:
            self._last = self._states.pop(0)
        return self._last


@pytest.fixture
def after_reply(settings: Settings, sink: CollectingEmailSink) -> LiveSession:
    """A session that has sent the clarification and merged the client's reply.

    One extractor scripted with both results, consumed in the order the session
    actually calls it: the enquiry on the first poll, and — because the second
    poll skips what it has already handled — the reply on the second.
    """
    session = LiveSession(
        settings,
        source=GrowingSource((ENQUIRY,), (ENQUIRY, REPLY)),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION, REPLY_EXTRACTION),
    )
    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()
    return session


def test_the_reply_validates_the_shipment_and_selects_a_rate(
    after_reply: LiveSession,
) -> None:
    request = only_request(after_reply)

    assert request.validation.is_valid is True  # type: ignore[attr-defined]
    assert request.state is RequestState.RATE_SELECTED  # type: ignore[attr-defined]
    assert request.packet is not None  # type: ignore[attr-defined]


def test_selecting_a_rate_sends_nothing_on_its_own(
    after_reply: LiveSession, sink: CollectingEmailSink
) -> None:
    """Searching and ranking are deterministic and contact nobody. Only the
    clarification has gone out; the gate has not been reached."""
    assert len(to_client(sink)) == 1
    assert to_approver(sink) == []


# --- the quotation gate ---------------------------------------------------------


def test_approving_sends_the_quotation_to_the_client(
    after_reply: LiveSession, sink: CollectingEmailSink
) -> None:
    request = after_reply.decide(
        only_request(after_reply).request_id,
        choice="approve",
        by=APPROVER,  # type: ignore[attr-defined]
    )

    assert request.quotation_sent is True
    assert request.state is RequestState.QUOTATION_SENT
    assert len(to_client(sink)) == 2  # clarification, then quotation
    assert len(to_approver(sink)) == 1  # the review packet


def test_declining_never_sends_the_quotation(
    after_reply: LiveSession, sink: CollectingEmailSink
) -> None:
    request = after_reply.decide(
        only_request(after_reply).request_id,  # type: ignore[attr-defined]
        choice="decline",
        by=APPROVER,
        reason="price too high",
    )

    assert request.quotation_sent is False
    assert request.state is RequestState.MAKER_REJECTED
    assert len(to_client(sink)) == 1  # the clarification only
    assert len(to_approver(sink)) == 1


@pytest.mark.parametrize("choice", ["", "yes", "maybe", "APPROVE ME", "true"])
def test_only_the_two_exact_words_are_decisions(
    after_reply: LiveSession, sink: CollectingEmailSink, choice: str
) -> None:
    """A malformed request can no more approve a quotation than decline one."""
    before = len(sink.sent)

    with pytest.raises(NotADecision):
        after_reply.decide(
            only_request(after_reply).request_id,
            choice=choice,
            by=APPROVER,  # type: ignore[attr-defined]
        )

    assert len(sink.sent) == before


def test_a_decision_cannot_be_recorded_anonymously(
    after_reply: LiveSession, sink: CollectingEmailSink
) -> None:
    with pytest.raises(NotADecision, match="name the person"):
        after_reply.decide(
            only_request(after_reply).request_id,
            choice="approve",
            by="  ",  # type: ignore[attr-defined]
        )

    assert len(to_client(sink)) == 1


def test_a_request_cannot_be_decided_twice(
    after_reply: LiveSession, sink: CollectingEmailSink
) -> None:
    request_id = only_request(after_reply).request_id  # type: ignore[attr-defined]
    after_reply.decide(request_id, choice="approve", by=APPROVER)
    before = len(sink.sent)

    with pytest.raises(LiveSequenceError, match="already been decided"):
        after_reply.decide(request_id, choice="approve", by=APPROVER)

    assert len(sink.sent) == before


def test_the_decision_is_persisted(after_reply: LiveSession, settings: Settings) -> None:
    request_id = only_request(after_reply).request_id  # type: ignore[attr-defined]
    after_reply.decide(request_id, choice="approve", by=APPROVER)

    stored = JsonFileStore(settings.demo.state_dir).get_request(request_id)
    assert stored is not None
    assert stored.state is RequestState.QUOTATION_SENT


def test_the_approver_is_recorded(after_reply: LiveSession) -> None:
    request = after_reply.decide(
        only_request(after_reply).request_id,
        choice="approve",
        by=APPROVER,  # type: ignore[attr-defined]
    )

    assert request.decision is not None
    assert request.decision.by == APPROVER


# --- the snapshot the browser receives ------------------------------------------


def test_simulated_rates_are_labelled_in_the_rate_view(after_reply: LiveSession) -> None:
    snap = live_serialize.snapshot(after_reply, selected=only_request(after_reply).request_id)  # type: ignore[attr-defined]

    rates = snap["selected"]["rates"]  # type: ignore[index]
    assert rates["simulated"] is True
    assert rates["banner"] == SIMULATED_BANNER


def test_simulated_rates_are_labelled_on_the_approval_card(
    after_reply: LiveSession,
) -> None:
    """The approver is deciding on invented numbers and must be told so by the
    same system that produced them."""
    snap = live_serialize.snapshot(after_reply, selected=only_request(after_reply).request_id)  # type: ignore[attr-defined]

    approval = snap["selected"]["approval"]  # type: ignore[index]
    assert approval["banner"] == SIMULATED_BANNER
    assert "not a commercial offer" in approval["notice"]


def test_the_approval_card_carries_everything_a_decision_needs(
    after_reply: LiveSession,
) -> None:
    snap = live_serialize.snapshot(after_reply, selected=only_request(after_reply).request_id)  # type: ignore[attr-defined]
    approval = snap["selected"]["approval"]  # type: ignore[index]

    for key in ("carrier", "service", "transit", "price", "reason", "excluded"):
        assert approval[key], f"{key} missing from the approval card"
    assert approval["review_sent_to"] == APPROVER_MAILBOX


def test_the_rate_view_reports_counts_and_exclusion_reasons(
    after_reply: LiveSession,
) -> None:
    snap = live_serialize.snapshot(after_reply, selected=only_request(after_reply).request_id)  # type: ignore[attr-defined]
    rates = snap["selected"]["rates"]  # type: ignore[index]

    assert rates["returned"] >= rates["eligible_count"] + rates["excluded_count"]
    assert rates["excluded"]
    for excluded in rates["excluded"]:
        assert excluded["reason"]
        assert excluded["detail"]


def test_the_shipment_view_shows_every_canonical_field(after_reply: LiveSession) -> None:
    snap = live_serialize.snapshot(after_reply, selected=only_request(after_reply).request_id)  # type: ignore[attr-defined]
    fields = {row["field"] for row in snap["selected"]["shipment"]}  # type: ignore[index]

    for expected in (
        "origin",
        "destination",
        "weight_kg",
        "dimensions_in",
        "commodity",
        "cargo_type",
        "is_chemical",
        "pcs",
        "delivery_type",
    ):
        assert expected in fields


def test_the_timeline_shows_the_gates_in_order(after_reply: LiveSession) -> None:
    after_reply.decide(
        only_request(after_reply).request_id,
        choice="approve",
        by=APPROVER,  # type: ignore[attr-defined]
    )
    snap = live_serialize.snapshot(after_reply)

    events = [entry["event"] for entry in snap["audit"]]  # type: ignore[index]
    assert events.index("approval_requested") < events.index("approval_decided")
    assert events.index("approval_decided") < events.index("quotation_sent")


def test_the_timeline_walks_the_named_stages_in_order(after_reply: LiveSession) -> None:
    snap = live_serialize.snapshot(after_reply, selected=only_request(after_reply).request_id)  # type: ignore[attr-defined]
    labels = [row["label"] for row in snap["selected"]["timeline"]]  # type: ignore[index]

    assert labels == [label for _, label, _, _, _ in live_serialize.TIMELINE]


def test_no_credential_or_auth_material_appears_in_any_snapshot(
    after_reply: LiveSession,
) -> None:
    after_reply.decide(
        only_request(after_reply).request_id,
        choice="approve",
        by=APPROVER,  # type: ignore[attr-defined]
    )
    rendered = json.dumps(
        live_serialize.snapshot(after_reply, selected=only_request(after_reply).request_id)  # type: ignore[attr-defined]
    )

    for marker in (FAKE_KEY, "api_key", "refresh_token", "client_secret", "Bearer", "token"):
        assert marker not in rendered


# --- the static and action surfaces ---------------------------------------------


def test_the_live_frontend_contains_no_unsafe_rendering_or_secrets() -> None:
    static = Path(live_serialize.__file__).resolve().parent / "static"
    html = (static / "live.html").read_text(encoding="utf-8")
    js = (static / "live.js").read_text(encoding="utf-8")

    for source in (html, js):
        assert ".innerHTML" not in source
        assert "insertAdjacentHTML" not in source
        assert "document.write" not in source
        assert "eval(" not in source
        assert "localStorage" not in source
        for marker in ("api_key", "sk-", "Bearer", "OPENROUTER", "refresh_token"):
            assert marker not in source
    assert "onclick=" not in html
    assert '<script src="/live.js" defer></script>' in html


def test_the_live_surfaces_are_closed_whitelists() -> None:
    assert set(_LIVE_FILES) == {
        "/",
        "/live.html",
        "/app.css",
        "/live.css",
        "/live.js",
        "/favicon.svg",
    }
    assert set(_LIVE_ACTIONS) == {
        "poll",
        "clarification/approve",
        "quotation/decide",
    }


def test_the_scripted_poc_surface_is_unchanged() -> None:
    """The live view is additive. The Phase 9 demo must still be exactly what
    it was, so it stays available if the mailbox misbehaves on the day."""
    assert set(_STATIC_FILES) == {"/", "/index.html", "/app.css", "/app.js", "/favicon.svg"}


# --- the server -----------------------------------------------------------------


@pytest.fixture
def live_server(settings: Settings, sink: CollectingEmailSink) -> Iterator[DemoServer]:
    instance = DemoServer(("127.0.0.1", 0), settings, live_session=session_for(settings, sink))
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()


def call(
    server: DemoServer, method: str, path: str, body: dict[str, object] | None = None
) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, {"raw": raw.decode("utf-8", "replace")}
    finally:
        connection.close()


def test_the_live_server_serves_the_live_page(live_server: DemoServer) -> None:
    status, payload = call(live_server, "GET", "/")
    page = str(payload["raw"])

    assert status == 200
    # The live view, not the scripted POC's page. Asserted on the script it
    # loads rather than on a badge: wording is a design decision and this test
    # is about which file is served.
    assert '<script src="/live.js" defer></script>' in page
    assert 'id="live-indicator"' in page


def test_the_live_server_refuses_paths_outside_the_whitelist(
    live_server: DemoServer,
) -> None:
    for path in ("/index.html", "/app.js", "/../server.py", "/nope"):
        status, _ = call(live_server, "GET", path)
        assert status == 404, path


def test_unknown_live_actions_do_not_exist(live_server: DemoServer) -> None:
    status, _ = call(live_server, "POST", "/api/live/send-quotation", {})

    assert status == 404


def test_the_whole_flow_runs_over_http(live_server: DemoServer, sink: CollectingEmailSink) -> None:
    status, snap = call(live_server, "POST", "/api/live/poll", {})
    assert status == 200
    assert sink.sent == []

    request_id = snap["requests"][0]["request_id"]  # type: ignore[index]
    status, _ = call(
        live_server,
        "POST",
        "/api/live/clarification/approve",
        {"by": APPROVER, "request_id": request_id},
    )
    assert status == 200
    assert len(to_client(sink)) == 1


def test_an_anonymous_decision_over_http_is_refused(live_server: DemoServer) -> None:
    call(live_server, "POST", "/api/live/poll", {})

    status, payload = call(
        live_server, "POST", "/api/live/quotation/decide", {"decision": "approve", "by": ""}
    )

    assert status == 409
    assert "name" in str(payload["error"]).lower() or "request" in str(payload["error"]).lower()


def test_live_endpoints_do_not_exist_on_the_scripted_server(settings: Settings) -> None:
    """A server started without --live has no live surface at all."""
    instance = DemoServer(("127.0.0.1", 0), settings)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        assert call(instance, "GET", "/api/live/state")[0] == 404
        assert call(instance, "POST", "/api/live/poll", {})[0] == 404
    finally:
        instance.shutdown()
        instance.server_close()


# --- classification: what makes a message a quotation enquiry -------------------


def noise(subject: str, sender: str) -> object:
    from translog_quote.domain.email import RawEmail

    return RawEmail(
        message_id=f"<{sender}@noise.example>",
        from_address=sender,
        subject=subject,
        body_text="Your weekly streak is alive! Keep playing to keep it going.",
        received_at=ENQUIRY.received_at,
    )


CHESS = noise("Your streak is on fire", "streaks@chess.com")
DISCORD = noise("Someone mentioned you", "noreply@discord.com")

#: What the model returns for a message that states no shipment: nothing.
#: Extraction may not fill a field the email did not state (BR-7), so this is
#: what an ordinary inbox email actually produces.
NOTHING_STATED = ExtractionResult()


def test_a_message_stating_no_shipment_is_not_an_enquiry(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The classification comes from extraction's own output, not a subject
    or sender list anybody has to maintain."""
    session = session_for(settings, sink, emails=(CHESS,), extractions=(NOTHING_STATED,))

    session.poll()

    request = only_request(session)
    assert request.shipment_field_count == 0  # type: ignore[attr-defined]
    assert request.looks_like_an_enquiry is False  # type: ignore[attr-defined]


def test_a_message_stating_a_shipment_is_an_enquiry(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    session = session_for(settings, sink)

    session.poll()

    request = only_request(session)
    assert request.shipment_field_count > 0  # type: ignore[attr-defined]
    assert request.looks_like_an_enquiry is True  # type: ignore[attr-defined]


def test_both_groups_are_listed_and_counted(settings: Settings, sink: CollectingEmailSink) -> None:
    """Unrecognised messages are shown, not hidden: the operator has to be able
    to check the classification rather than trust it."""
    session = session_for(
        settings,
        sink,
        emails=(CHESS, ENQUIRY, DISCORD),
        extractions=(NOTHING_STATED, ENQUIRY_EXTRACTION, NOTHING_STATED),
    )

    session.poll()
    snap = live_serialize.snapshot(session)

    assert len(snap["requests"]) == 3  # type: ignore[arg-type]
    assert snap["poll"]["enquiries"] == 1  # type: ignore[index]
    assert snap["poll"]["unrecognised"] == 2  # type: ignore[index]


def test_an_unrecognised_message_explains_itself(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    session = session_for(settings, sink, emails=(CHESS,), extractions=(NOTHING_STATED,))
    session.poll()

    summary = live_serialize.snapshot(session)["requests"][0]  # type: ignore[index]

    assert summary["is_enquiry"] is False
    assert summary["shipment_fields"] == 0
    assert "No shipment details found" in summary["not_enquiry_reason"]


def test_an_enquiry_carries_no_not_enquiry_reason(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    session = session_for(settings, sink)
    session.poll()

    summary = live_serialize.snapshot(session)["requests"][0]  # type: ignore[index]

    assert summary["is_enquiry"] is True
    assert summary["not_enquiry_reason"] is None


def test_nothing_is_mailed_to_an_unrecognised_sender(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """Polling never sends. The clarification for a non-enquiry exists as a
    draft and stays one — the interface does not offer to release it."""
    session = session_for(settings, sink, emails=(CHESS,), extractions=(NOTHING_STATED,))

    session.poll()

    assert sink.sent == []


def test_an_unrecognised_message_leaves_no_request_on_disk(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """Only the thread anchor is committed. A dead NEEDS_INFO row that can
    neither advance nor be explained never reaches the store."""
    session = session_for(settings, sink, emails=(CHESS,), extractions=(NOTHING_STATED,))
    session.poll()

    store = JsonFileStore(settings.demo.state_dir)
    assert store.all_threads()  # it was seen
    assert store.get_request(only_request(session).request_id) is None  # type: ignore[attr-defined]


def test_an_unrecognised_message_is_never_extracted_twice(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The scripted extractor holds one result, so a second call would raise.

    This is what the thread-only commit buys: an inbox full of newsletters
    costs one model call each, once, rather than once per poll forever.
    """
    session = session_for(settings, sink, emails=(CHESS,), extractions=(NOTHING_STATED,))
    session.poll()

    session.poll()

    assert session.last_poll_new == 0


def test_a_restarted_session_does_not_resurrect_unrecognised_messages(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    session = session_for(settings, sink, emails=(CHESS,), extractions=(NOTHING_STATED,))
    session.poll()

    restarted = LiveSession(
        settings,
        source=StubSource(),  # type: ignore[arg-type]
        sink=CollectingEmailSink(),
        extractor=ScriptedExtractor(),
    )

    assert restarted.requests == {}


def test_an_enquiry_and_its_reply_still_flow_past_the_classifier(
    after_reply: LiveSession, sink: CollectingEmailSink
) -> None:
    """The classification must not become a gate. A real conversation reaches
    the approval card exactly as before."""
    request = only_request(after_reply)

    assert request.looks_like_an_enquiry is True  # type: ignore[attr-defined]
    assert request.packet is not None  # type: ignore[attr-defined]


# --- timestamps: real, or absent ------------------------------------------------


def test_the_live_session_runs_on_the_wall_clock(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The fixed clock would stamp every event with the same invented moment,
    which is precisely the fabricated timestamp the interface must not show."""
    from translog_quote.adapters.clock import DEMO_EPOCH

    session = session_for(settings, sink)
    session.poll()

    stamps = {event.at for event in session.audit.events}
    assert stamps
    assert DEMO_EPOCH not in stamps


def test_the_card_carries_the_emails_own_received_time(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """Not the moment we processed it — the moment the client wrote."""
    session = session_for(settings, sink)
    session.poll()

    summary = live_serialize.snapshot(session)["requests"][0]  # type: ignore[index]
    assert summary["received_at"] == ENQUIRY.received_at.isoformat()


def test_the_card_shows_the_subject_and_a_headline(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """A card must be useful before extraction has filled anything in."""
    demo_enquiry = ENQUIRY.model_copy(
        update={"subject": "Air Freight Quote Demo - Mumbai to Dubai"}
    )
    session = session_for(settings, sink, emails=(demo_enquiry,))
    session.poll()

    summary = live_serialize.snapshot(session)["requests"][0]  # type: ignore[index]
    assert summary["subject"] == "Air Freight Quote Demo - Mumbai to Dubai"
    assert summary["headline"] == "Air Freight Quote Demo"


def test_a_reply_headline_drops_the_reply_prefix(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    from translog_quote.interface.web.live_session import LiveRequest

    request = LiveRequest(
        request_id="R-1",
        client_address=CLIENT,
        state=RequestState.RECEIVED,
        record=ENQUIRY_EXTRACTION and None,  # type: ignore[arg-type]
        validation=None,  # type: ignore[arg-type]
        subject="Re: Air Freight Quote Demo - Mumbai to Dubai",
    )

    assert live_serialize._headline(request) == "Air Freight Quote Demo"


# --- the activity timeline ------------------------------------------------------


def timeline_of(session: LiveSession) -> list[dict[str, object]]:
    snap = live_serialize.snapshot(session, selected=only_request(session).request_id)  # type: ignore[attr-defined]
    return snap["selected"]["timeline"]  # type: ignore[index,return-value]


def test_the_timeline_marks_only_what_actually_happened(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """Nothing is hardcoded as complete. Straight after the enquiry, the later
    stages have no events and must read as pending."""
    session = session_for(settings, sink)
    session.poll()

    rows = {row["key"]: row for row in timeline_of(session)}
    assert rows["enquiry_received"]["state"] == "done"
    assert rows["extraction"]["state"] == "done"
    assert rows["validation"]["state"] == "done"
    assert rows["reply_received"]["state"] == "pending"
    assert rows["quotation_sent"]["state"] == "pending"


def test_every_completed_timeline_row_carries_a_real_timestamp(
    after_reply: LiveSession,
) -> None:
    for row in timeline_of(after_reply):
        if row["state"] == "done":
            assert row["at"], f"{row['key']} is done but has no timestamp"


def test_no_pending_timeline_row_invents_a_timestamp(after_reply: LiveSession) -> None:
    """A timeline that invents its own history is worse than one with gaps."""
    for row in timeline_of(after_reply):
        if row["state"] != "done":
            assert row["at"] is None


def test_the_timeline_names_what_the_request_is_waiting_for(
    after_reply: LiveSession,
) -> None:
    current = [row for row in timeline_of(after_reply) if row["state"] == "current"]

    assert len(current) == 1
    assert current[0]["key"] == "approval_decided"
    assert current[0]["note"] == "Waiting for approval"


def test_the_timeline_completes_once_the_quotation_is_sent(
    after_reply: LiveSession,
) -> None:
    after_reply.decide(
        only_request(after_reply).request_id,
        choice="approve",
        by=APPROVER,  # type: ignore[attr-defined]
    )

    rows = {row["key"]: row for row in timeline_of(after_reply)}
    assert rows["approval_decided"]["state"] == "done"
    assert rows["quotation_sent"]["state"] == "done"
    assert rows["quotation_sent"]["at"]


def test_a_decline_leaves_the_quotation_row_unreached(after_reply: LiveSession) -> None:
    after_reply.decide(
        only_request(after_reply).request_id,
        choice="decline",
        by=APPROVER,  # type: ignore[attr-defined]
    )

    rows = {row["key"]: row for row in timeline_of(after_reply)}
    assert rows["approval_decided"]["state"] == "done"
    assert rows["quotation_sent"]["state"] != "done"
    assert rows["quotation_sent"]["at"] is None


def test_the_timeline_survives_a_restart(settings: Settings, sink: CollectingEmailSink) -> None:
    """The audit log is persisted, so a restarted server still shows what
    happened rather than an empty history for a request that progressed."""
    session = session_for(settings, sink)
    session.poll()
    session.approve_clarification(by=APPROVER)
    before = len(session.audit.events)

    restarted = LiveSession(
        settings,
        source=StubSource(),  # type: ignore[arg-type]
        sink=CollectingEmailSink(),
        extractor=ScriptedExtractor(),
    )

    assert len(restarted.audit.events) == before
    rows = {row["key"]: row for row in timeline_of(restarted)}
    assert rows["clarification_sent"]["state"] == "done"
    assert rows["clarification_sent"]["at"]


# --- the capability strip is gone; the rate disclosure is not -------------------


def test_the_capability_strip_is_not_in_the_payload(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The "Email inbound REAL / Rate provider SIMULATED" strip was internal
    mechanics on
    a client-facing screen. Removed from the payload as well as from the page:
    leaving the data in and hiding it in the browser is how a "removed" feature
    comes back."""
    session = session_for(settings, sink)

    assert "provenance" not in live_serialize.snapshot(session)["mode"]  # type: ignore[operator]


def test_the_capability_strip_is_gone_from_the_page() -> None:
    static = Path(live_serialize.__file__).resolve().parent / "static"
    html = (static / "live.html").read_text(encoding="utf-8")
    js = (static / "live.js").read_text(encoding="utf-8")

    assert 'id="provenance"' not in html
    assert "provenanceStrip" not in js


def test_no_developer_readout_survives_on_the_page() -> None:
    """The dashboard is for somebody quoting a shipment, not for whoever wrote
    it. Mailbox counts, poll timings and extraction bookkeeping are removed
    from the page — the values stay in the payload, where the liveness
    indicator and the tests can still read them."""
    static = Path(live_serialize.__file__).resolve().parent / "static"
    html = (static / "live.html").read_text(encoding="utf-8")
    js = (static / "live.js").read_text(encoding="utf-8")

    for readout in (
        "older mailbox message(s) not read",
        "new message(s) last check",
        "internal mail skipped",
        "Shipment details found",
        "request(s) received since this session started",
        "send-only credential",
        "RFC IN-REPLY-TO",
    ):
        assert readout not in js, readout
    assert 'id="demo-scope"' not in html
    assert 'id="poll-note"' not in html


def test_the_simulated_rate_disclosure_survives_the_cleanup(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The strip went; the thing that actually matters did not. A simulated
    price must still announce itself wherever it is shown."""
    session = session_for(settings, sink)

    assert live_serialize.snapshot(session)["mode"]["banner"] == SIMULATED_BANNER  # type: ignore[index]


def test_one_held_draft_does_not_block_every_other_message(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """Requests are independent, so a block on one must not stop the rest.

    The guard here used to stop the whole loop, which meant a single ordinary
    inbox message holding a clarification draft hid every message behind it —
    including the enquiry the demonstration is about.
    """
    session = session_for(
        settings,
        sink,
        emails=(CHESS, ENQUIRY, DISCORD),
        extractions=(NOTHING_STATED, ENQUIRY_EXTRACTION, NOTHING_STATED),
    )

    session.poll()

    assert len(session.requests) == 3
    assert any(request.looks_like_an_enquiry for request in session.requests.values())


def test_a_reply_waits_for_its_own_clarification_to_be_sent(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The per-request block is still real: a reply cannot be processed while
    its own request is holding an unsent draft, because the table permits no
    way out of NEEDS_INFO except CLARIFICATION_SENT."""
    session = LiveSession(
        settings,
        source=StubSource(ENQUIRY, REPLY),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION, REPLY_EXTRACTION),
    )

    session.poll()

    assert session.blocked_messages == 1
    assert only_request(session).reply_received is False  # type: ignore[attr-defined]
    assert sink.sent == []


def test_the_deferred_reply_is_processed_once_the_clarification_goes_out(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """Three scripted results for two messages, and that is not a typo.

    A deferred reply costs one extraction: the clarification loop calls the
    model before it checks the transition, so the blocked attempt has already
    paid for a call by the time it is refused. The alternative would be
    reordering `ClarificationWorkflow`, which is business code this change does
    not touch — so the cost is real, bounded to one call per premature poll,
    and recorded here rather than hidden.
    """
    session = LiveSession(
        settings,
        source=StubSource(ENQUIRY, REPLY),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION, REPLY_EXTRACTION, REPLY_EXTRACTION),
    )
    session.poll()
    session.approve_clarification(by=APPROVER)

    session.poll()

    assert session.blocked_messages == 0
    assert only_request(session).reply_received is True  # type: ignore[attr-defined]


# --- repeat polls must not re-extract open enquiries ----------------------------


def test_a_second_poll_does_not_re_extract_an_open_enquiry(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The regression behind a Check mail that looks broken.

    An enquiry waiting on its clarification is deliberately not persisted, so
    the durable store cannot say it has been seen. Without an in-session
    record, every Check mail re-extracted every open enquiry at one live model
    call each — turning a click into half a minute of apparent silence, and
    getting slower as the mailbox filled.

    The scripted extractor holds one result, so a second call raises.
    """
    session = session_for(settings, sink)
    session.poll()
    assert only_request(session).state is RequestState.NEEDS_INFO  # type: ignore[attr-defined]

    session.poll()

    assert session.last_poll_new == 0
    assert len(session.requests) == 1


def test_a_deferred_message_is_still_retried_on_the_next_poll(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """A message that could not be processed is not marked as seen. It has to
    come back once the clarification blocking it has actually been sent."""
    session = LiveSession(
        settings,
        source=StubSource(ENQUIRY, REPLY),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION, REPLY_EXTRACTION, REPLY_EXTRACTION),
    )
    session.poll()
    assert session.blocked_messages == 1

    session.approve_clarification(by=APPROVER)
    session.poll()

    assert session.last_poll_new == 1  # the reply came back
    assert only_request(session).reply_received is True  # type: ignore[attr-defined]


def test_new_mail_still_arrives_after_a_quiet_poll(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """Deduplication must not become a filter that stops seeing the mailbox."""
    session = LiveSession(
        settings,
        source=GrowingSource((ENQUIRY,), (ENQUIRY,), (ENQUIRY, REPLY)),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION, REPLY_EXTRACTION),
    )
    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()  # nothing new

    session.poll()  # the reply has arrived

    assert only_request(session).reply_received is True  # type: ignore[attr-defined]


# --- the error boundary ---------------------------------------------------------


def test_a_non_translog_failure_still_answers_with_json(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not every failure below the handler is a `TranslogError`.

    The original instance was `UnknownPlace`, a ValueError the routing table
    raised for any lane nobody had added. Uncaught it escaped as an empty 500,
    the browser's `response.json()` threw, and the page reported that the
    server had not responded — wrong, and unactionable. The table is gone; the
    handler's contract is not, so a plain exception is still asserted here."""
    from translog_quote.interface.web.live_session import LiveSession as Live

    def explode(self: object) -> None:
        raise ValueError("'Atlantis' is not a place this stub understands")

    monkeypatch.setattr(Live, "poll", explode)

    status, payload = call(live_server, "POST", "/api/live/poll", {})

    assert status == 500
    assert payload["error"] == "ValueError"


def test_an_error_response_names_the_class_and_nothing_else(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adapter messages can carry provider detail that does not belong in a
    browser, so the response says what kind of failure it was and no more."""
    from translog_quote.interface.web.live_session import LiveSession as Live

    def explode(self: object) -> None:
        raise RuntimeError("Bearer sk-secret-token leaked into a message")

    monkeypatch.setattr(Live, "poll", explode)

    status, payload = call(live_server, "POST", "/api/live/poll", {})

    assert status == 500
    assert payload == {"error": "RuntimeError"}


# --- the page reads the mailbox by no action of anyone's ------------------------


def test_no_manual_mailbox_control_exists_on_the_page() -> None:
    """K. Not "the button is hidden": there is no control, no handler and no
    call. The mailbox is read by the server's own poller, so a demonstration
    cannot stall on somebody not knowing to click."""
    static = Path(live_serialize.__file__).resolve().parent / "static"
    html = (static / "live.html").read_text(encoding="utf-8")
    js = (static / "live.js").read_text(encoding="utf-8")

    assert "Check mail" not in html
    assert "btn-poll" not in html
    assert "Start new demonstration" not in html
    assert "btn-new-demo" not in html
    assert "checkMail" not in js
    assert '"poll"' not in js, "nothing on the page asks the server to read the mailbox"
    assert "demonstration/start" not in js


def test_the_page_watches_for_changes_on_a_timer() -> None:
    """L, at the level source can prove: the page arms a repeating refresh at
    load. That it actually redraws on new state is driven for real in
    tests/js/live_ui.test.js."""
    static = Path(live_serialize.__file__).resolve().parent / "static"
    js = (static / "live.js").read_text(encoding="utf-8")

    assert "REFRESH_MS" in js
    assert "setInterval" in js
    assert "watchForChanges();" in js


def test_the_frontend_bounds_how_long_it_will_wait() -> None:
    """A poll is one live model call per unread message. Without a ceiling a
    stalled request leaves the page waiting forever with no way to tell that
    apart from slow."""
    static = Path(live_serialize.__file__).resolve().parent / "static"
    js = (static / "live.js").read_text(encoding="utf-8")

    assert "POLL_TIMEOUT_MS" in js
    assert "AbortController" in js


def test_a_blocked_reply_is_deferred_without_calling_the_model(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The clarification loop calls the model before it checks the transition,
    so letting a blocked reply through costs a live call every poll. The
    scripted extractor holds exactly one result: a second call would raise."""
    session = LiveSession(
        settings,
        source=StubSource(ENQUIRY, REPLY),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION),
    )

    session.poll()

    assert session.blocked_messages == 1
    assert only_request(session).reply_received is False  # type: ignore[attr-defined]


def test_repeated_polls_cost_nothing_while_a_conversation_waits(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """Check mail must not get slower the longer a conversation stays open."""
    session = LiveSession(
        settings,
        source=StubSource(ENQUIRY, REPLY),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION),
    )
    session.poll()

    session.poll()
    session.poll()

    assert session.blocked_messages == 1


def test_the_reply_is_processed_once_its_clarification_has_gone_out(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """Deferral must not become a dead end: once the block is lifted the reply
    is routed normally, and only then does it cost an extraction."""
    session = LiveSession(
        settings,
        source=StubSource(ENQUIRY, REPLY),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION, REPLY_EXTRACTION),
    )
    session.poll()
    session.approve_clarification(by=APPROVER)

    session.poll()

    assert session.blocked_messages == 0
    assert only_request(session).reply_received is True  # type: ignore[attr-defined]


# --- the clarification row must not claim to be sent before it is ---------------


def clarification_row(session: LiveSession) -> dict[str, object]:
    rows = timeline_of(session)
    return next(row for row in rows if row["key"] == "clarification_sent")


def test_an_unsent_clarification_does_not_read_as_sent(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The row used to read "Clarification sent" above "Waiting for a person to
    approve and send" — a contradiction, and the half a presenter reads aloud
    is the wrong half."""
    session = session_for(settings, sink)
    session.poll()

    row = clarification_row(session)

    assert row["label"] == "Clarification awaiting approval"
    assert row["note"] == "Waiting for a person to approve and send"
    assert row["state"] == "current"
    assert row["at"] is None


def test_the_row_reads_sent_only_once_the_backend_confirms_it(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """`CLARIFICATION_SENT` is emitted by the clarification loop *after* the
    sink accepted the message, so the event is real confirmation rather than an
    intention. Nothing in the interface decides this."""
    session = session_for(settings, sink)
    session.poll()
    assert clarification_row(session)["label"] == "Clarification awaiting approval"

    session.approve_clarification(by=APPROVER)

    row = clarification_row(session)
    assert row["label"] == "Clarification sent"
    assert row["state"] == "done"
    assert row["at"]


def test_the_next_step_then_waits_on_the_client(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    session = session_for(settings, sink)
    session.poll()
    session.approve_clarification(by=APPROVER)

    rows = {row["key"]: row for row in timeline_of(session)}

    assert rows["reply_received"]["state"] == "current"
    assert rows["reply_received"]["note"] == "Waiting for client reply"


def test_the_label_follows_the_audit_event_and_not_the_request_state(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """Source of truth check. A session restored from disk has the persisted
    CLARIFICATION_SENT state and the persisted audit event; the row reads
    "sent" because the event exists, not because a status string was stored."""
    session = session_for(settings, sink)
    session.poll()
    session.approve_clarification(by=APPROVER)

    restarted = LiveSession(
        settings,
        source=StubSource(),  # type: ignore[arg-type]
        sink=CollectingEmailSink(),
        extractor=ScriptedExtractor(),
    )

    sent = [e for e in restarted.audit.events if e.event.value == "clarification_sent"]
    assert sent, "the confirming event must survive the restart"
    assert clarification_row(restarted)["label"] == "Clarification sent"


# --- the clarification approval still reaches the real Gmail send path ----------


def test_a_live_session_sends_through_the_gmail_sink_by_default(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interface must not have quietly acquired a sink of its own. Built
    without an explicit one, the session takes the composition root's Gmail
    sink — the same send-only credential the CLI uses."""
    built = CollectingEmailSink()
    calls: list[str] = []

    def fake_sink(_settings: Settings) -> CollectingEmailSink:
        calls.append("built")
        return built

    monkeypatch.setattr(bootstrap, "build_gmail_email_sink", fake_sink)

    session = LiveSession(
        settings,
        source=StubSource(ENQUIRY),  # type: ignore[arg-type]
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION),
    )
    session.poll()
    session.approve_clarification(by=APPROVER)

    assert calls == ["built"]
    assert [m.to_address for m in built.sent] == [CLIENT]


def test_the_clarification_gate_is_the_workflows_own_and_not_a_second_one(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The interface calls through to `ClarificationWorkflow.approve_clarification`
    — the gate that composes the message, records who approved, hands it to the
    sink and only then advances the state. A clarification cannot leave by any
    other route, and the audit trail proves which one it took."""
    session = session_for(settings, sink)
    session.poll()

    session.approve_clarification(by=APPROVER)

    events = [e.event.value for e in session.audit.events]
    assert events.index("clarification_approved") < events.index("clarification_sent")
    approved = next(e for e in session.audit.events if e.event.value == "clarification_approved")
    assert approved.detail["by"] == APPROVER


# --- the clarification approval, over HTTP --------------------------------------


@pytest.fixture
def awaiting_server(
    settings: Settings, sink: CollectingEmailSink
) -> Iterator[tuple[DemoServer, CollectingEmailSink, str]]:
    """A live server whose one request is holding a clarification draft."""
    session = session_for(settings, sink)
    session.poll()
    request_id = only_request(session).request_id  # type: ignore[attr-defined]
    instance = DemoServer(("127.0.0.1", 0), settings, live_session=session)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance, sink, request_id
    instance.shutdown()
    instance.server_close()


def test_the_api_reports_the_request_as_awaiting_clarification_approval(
    awaiting_server: tuple[DemoServer, CollectingEmailSink, str],
) -> None:
    """What the button's availability is ultimately answering. If this were
    false the control would correctly not be offered at all."""
    server, _, request_id = awaiting_server

    _, snap = call(server, "GET", f"/api/live/state?request_id={request_id}")

    assert snap["selected"]["clarification"]["awaiting_approval"] is True  # type: ignore[index]
    assert snap["selected"]["is_enquiry"] is True  # type: ignore[index]
    assert snap["selected"]["request_id"] == request_id  # type: ignore[index]


def test_approving_over_http_sends_the_clarification(
    awaiting_server: tuple[DemoServer, CollectingEmailSink, str],
) -> None:
    server, sink, request_id = awaiting_server

    status, _ = call(
        server,
        "POST",
        "/api/live/clarification/approve",
        {"by": "Aayush", "request_id": request_id},
    )

    assert status == 200
    assert [m.to_address for m in sink.sent] == [CLIENT]


def test_an_anonymous_clarification_approval_is_refused_and_sends_nothing(
    awaiting_server: tuple[DemoServer, CollectingEmailSink, str],
) -> None:
    """The interface disables the button, and the server refuses regardless —
    the guarantee does not rest on the frontend behaving."""
    server, sink, request_id = awaiting_server

    status, payload = call(
        server,
        "POST",
        "/api/live/clarification/approve",
        {"by": "   ", "request_id": request_id},
    )

    assert status == 409
    assert "named person" in str(payload["error"])
    assert sink.sent == []


def test_no_quotation_is_sent_at_the_clarification_stage(
    awaiting_server: tuple[DemoServer, CollectingEmailSink, str],
) -> None:
    """Approving a clarification releases exactly one message, to the client,
    and touches neither the approver mailbox nor the quotation gate."""
    server, sink, request_id = awaiting_server

    call(
        server,
        "POST",
        "/api/live/clarification/approve",
        {"by": "Aayush", "request_id": request_id},
    )

    assert len(sink.sent) == 1
    assert to_approver(sink) == []
    _, snap = call(server, "GET", f"/api/live/state?request_id={request_id}")
    assert snap["selected"]["approval"] is None  # type: ignore[index]
    assert snap["selected"]["decision"] is None  # type: ignore[index]


def test_nothing_is_sent_before_the_click(
    awaiting_server: tuple[DemoServer, CollectingEmailSink, str],
) -> None:
    """Reading the state as often as the interface likes must send nothing."""
    server, sink, request_id = awaiting_server

    for _ in range(3):
        call(server, "GET", f"/api/live/state?request_id={request_id}")

    assert sink.sent == []


# --- the phantom reply: an enquiry re-ingested is not a client reply -------------


def test_re_ingesting_the_same_enquiry_is_not_a_client_reply(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The bug this class of test exists for.

    A request awaiting its clarification is deliberately not persisted, so a
    new session re-ingests the same enquiry and appends a second
    `email_received` for the very same Message-ID. The timeline identified the
    reply as "the second email_received", so the screen showed "Client reply
    received" beneath "Clarification awaiting approval" — two things that
    cannot both be true, on a request whose client had not replied at all.
    """
    first = session_for(settings, sink)
    first.poll()

    second = session_for(settings, sink)  # a fresh process, same mailbox
    second.poll()

    events = [e for e in second.audit.events if e.event.value == "email_received"]
    assert len(events) == 2, "the same enquiry really is recorded twice"
    assert len({e.detail["message_id"] for e in events}) == 1, "but it is one message"

    rows = {row["key"]: row for row in timeline_of(second)}
    assert rows["reply_received"]["state"] != "done"
    assert rows["reply_received"]["at"] is None


def test_the_clarification_row_and_the_reply_row_cannot_contradict(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """A reply cannot have been received into a request whose clarification was
    never sent — the table permits no way out of NEEDS_INFO except
    CLARIFICATION_SENT. The screen must not claim otherwise."""
    session = session_for(settings, sink)
    session.poll()
    session_for(settings, sink).poll()  # a second session, re-ingesting

    rows = {row["key"]: row for row in timeline_of(session)}

    if rows["clarification_sent"]["state"] != "done":
        assert rows["reply_received"]["state"] != "done"


def test_a_genuine_reply_is_still_recognised(
    after_reply: LiveSession,
) -> None:
    """Counting distinct messages must not stop a real second message counting."""
    rows = {row["key"]: row for row in timeline_of(after_reply)}

    assert rows["reply_received"]["state"] == "done"
    assert rows["reply_received"]["at"] == REPLY.received_at.isoformat()


def test_an_email_row_uses_the_messages_own_date_header(
    after_reply: LiveSession,
) -> None:
    """Email stages keep real Date headers; processing stages keep wall clock."""
    rows = {row["key"]: row for row in timeline_of(after_reply)}

    assert rows["enquiry_received"]["at"] == ENQUIRY.received_at.isoformat()
    assert rows["reply_received"]["at"] == REPLY.received_at.isoformat()
    # A processing stage is stamped when it ran, not by any header.
    assert rows["validation"]["at"] not in {
        ENQUIRY.received_at.isoformat(),
        REPLY.received_at.isoformat(),
    }


# --- a reply waiting behind an unsent clarification -----------------------------


def test_a_reply_waiting_on_a_clarification_is_reported(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The refusal is correct and used to be silent: the operator saw a request
    that looked idle while their client was waiting, and no reason why."""
    session = LiveSession(
        settings,
        source=StubSource(ENQUIRY, REPLY),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION),
    )

    session.poll()

    request = only_request(session)
    assert request.waiting_replies == [REPLY.message_id]  # type: ignore[attr-defined]
    snap = live_serialize.snapshot(session, selected=request.request_id)  # type: ignore[attr-defined]
    assert snap["requests"][0]["waiting_replies"] == 1  # type: ignore[index]
    assert snap["selected"]["waiting_replies"] == 1  # type: ignore[index]


def test_the_waiting_reply_clears_once_it_has_been_merged(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """Recomputed each poll, so a reply that has since been merged stops being
    reported as waiting — a stale banner is its own kind of wrong."""
    session = LiveSession(
        settings,
        source=StubSource(ENQUIRY, REPLY),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION, REPLY_EXTRACTION),
    )
    session.poll()
    assert only_request(session).waiting_replies  # type: ignore[attr-defined]

    session.approve_clarification(by=APPROVER)
    session.poll()

    assert only_request(session).waiting_replies == []  # type: ignore[attr-defined]


# --- the full visual path: clarification -> reply -> advance --------------------


def test_check_mail_advances_the_request_past_the_clarification_gate(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The whole reported journey, through the visual path only.

    Clarification sent, client replies, Check mail processes it, and the
    request leaves the clarification stage instead of sitting on it.
    """
    session = LiveSession(
        settings,
        source=GrowingSource((ENQUIRY,), (ENQUIRY, REPLY)),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION, REPLY_EXTRACTION),
    )

    session.poll()
    assert only_request(session).awaiting_clarification_approval is True  # type: ignore[attr-defined]

    session.approve_clarification(by=APPROVER)
    session.poll()  # "Check mail" after the client replies

    request = only_request(session)
    assert request.awaiting_clarification_approval is False  # type: ignore[attr-defined]
    assert request.reply_received is True  # type: ignore[attr-defined]
    assert request.state is RequestState.RATE_SELECTED  # type: ignore[attr-defined]
    assert request.packet is not None  # type: ignore[attr-defined]


def test_no_stale_clarification_gate_survives_a_processed_reply(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """After the reply is merged the interface must not still be offering to
    send a clarification, and the timeline must have moved on."""
    session = LiveSession(
        settings,
        source=GrowingSource((ENQUIRY,), (ENQUIRY, REPLY)),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION, REPLY_EXTRACTION),
    )
    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()

    request = only_request(session)
    snap = live_serialize.snapshot(session, selected=request.request_id)  # type: ignore[attr-defined]
    detail = snap["selected"]

    # Stronger than "the gate is closed": the reply turn produced no new
    # clarification, so there is no clarification section to render at all.
    assert detail["clarification"] is None  # type: ignore[index]
    assert detail["waiting_replies"] == 0  # type: ignore[index]
    assert detail["approval"] is not None  # type: ignore[index]

    rows = {row["key"]: row for row in detail["timeline"]}  # type: ignore[index]
    for key in ("clarification_sent", "reply_received", "rate_search", "rate_selected"):
        assert rows[key]["state"] == "done", key
    assert rows["approval_decided"]["state"] == "current"


def test_the_visual_path_and_the_cli_path_agree_on_the_reply(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """Both drive the same InboundRouter and workflow, so a correlated reply
    must produce the same merge either way."""
    session = LiveSession(
        settings,
        source=GrowingSource((ENQUIRY,), (ENQUIRY, REPLY)),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION, REPLY_EXTRACTION),
    )
    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()

    record = only_request(session).record  # type: ignore[attr-defined]
    # Carried from the enquiry, and supplied by the reply — the merge the CLI
    # path produces for this same conversation.
    assert record.origin == "Ahmedabad"
    assert record.weight_kg == 500.0
    assert record.is_chemical is False
    assert record.delivery_type is not None


# --- the demonstration scope ----------------------------------------------------


def older(email: object, minutes: int) -> object:
    """The same message, received earlier."""
    return email.model_copy(  # type: ignore[attr-defined]
        update={"received_at": email.received_at - timedelta(minutes=minutes)}  # type: ignore[attr-defined]
    )


def scoped_session(
    settings: Settings,
    sink: CollectingEmailSink,
    *,
    emails: tuple[object, ...],
    extractions: tuple[object, ...],
) -> LiveSession:
    """A session whose clock reads exactly when the fixture enquiry arrived.

    The fixture emails are dated later than the wall clock, so "older than now"
    would not be older than them. Pinning the clock to the enquiry's own
    arrival makes `older(...)` mean what it says and the cutoff testable.
    """
    from translog_quote.adapters.clock import FixedClock

    return LiveSession(
        settings,
        source=StubSource(*emails),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(*extractions),  # type: ignore[arg-type]
        clock=FixedClock(ENQUIRY.received_at),
    )


def test_nothing_is_filtered_until_a_demonstration_is_started(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The default must be exactly the behaviour that existed before."""
    session = session_for(settings, sink)

    session.poll()

    assert session.demonstration.is_active is False
    assert session.outside_demonstration == 0
    assert len(session.requests) == 1
    assert session.in_demonstration(only_request(session).request_id) is True  # type: ignore[attr-defined]


def test_a_started_demonstration_does_not_read_older_mail(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """Older mail is left unread, not read-and-hidden. Extracting history to
    then not show it would cost a live model call each."""
    session = scoped_session(
        settings, sink, emails=(older(ENQUIRY, 90),), extractions=()
    )  # a single extraction call would raise
    session.start_demonstration()

    session.poll()

    assert session.outside_demonstration == 1
    assert session.requests == {}


def test_the_fresh_enquiry_is_the_one_followed(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """The whole point: a mailbox full of history, and the demonstration
    follows the message that arrived after Start."""
    session = scoped_session(
        settings, sink, emails=(older(ENQUIRY, 120), ENQUIRY), extractions=(ENQUIRY_EXTRACTION,)
    )
    session.start_demonstration()

    session.poll()

    assert session.outside_demonstration == 1
    assert len(session.requests) == 1
    assert session.in_demonstration(only_request(session).request_id) is True  # type: ignore[attr-defined]


def test_an_old_reply_cannot_attach_itself_to_a_new_demonstration(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """Structural rather than a rule anyone has to remember: a reply that
    predates the cutoff is never read, so it cannot be merged into anything."""
    session = scoped_session(
        settings, sink, emails=(older(REPLY, 300), ENQUIRY), extractions=(ENQUIRY_EXTRACTION,)
    )
    session.start_demonstration()

    session.poll()

    assert session.outside_demonstration == 1
    assert only_request(session).reply_received is False  # type: ignore[attr-defined]


def test_a_request_from_before_the_demonstration_leaves_the_live_view(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """E, at the session level. A new session leads with its own work and
    nothing else — and it drops the earlier request from the *session*, not
    from the page: a request left in `session.requests` is still walked by the
    rate-search pass on every background poll, so filtering it in the browser
    would hide work that was still being done."""
    session = session_for(settings, sink)
    session.poll()
    old_id = only_request(session).request_id  # type: ignore[attr-defined]

    session.start_demonstration()

    assert session.in_demonstration(old_id) is False
    assert old_id not in session.requests, "it is no longer active work"
    assert live_serialize.snapshot(session)["requests"] == []


def test_starting_a_demonstration_deletes_no_persisted_work(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    session = session_for(settings, sink)
    session.poll()
    session.approve_clarification(by=APPROVER)
    before = JsonFileStore(settings.demo.state_dir).all_threads()

    session.start_demonstration()

    assert JsonFileStore(settings.demo.state_dir).all_threads() == before


def test_the_focus_survives_a_restart(settings: Settings, sink: CollectingEmailSink) -> None:
    """Membership is recorded, not re-derived from timestamps a restarted
    server may no longer hold."""
    session = scoped_session(settings, sink, emails=(ENQUIRY,), extractions=(ENQUIRY_EXTRACTION,))
    session.start_demonstration()
    session.poll()
    request_id = only_request(session).request_id  # type: ignore[attr-defined]
    session.approve_clarification(by=APPROVER)  # commits it, so a restart can see it

    restarted = LiveSession(
        settings,
        source=StubSource(),  # type: ignore[arg-type]
        sink=CollectingEmailSink(),
        extractor=ScriptedExtractor(),
    )

    assert restarted.demonstration.is_active is True
    assert restarted.in_demonstration(request_id) is True
    assert request_id in restarted.requests, "a followed request is still restored"


# --- what the dashboard is told -------------------------------------------------


def test_the_snapshot_carries_this_session_and_admits_what_it_skipped(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """E. One active request on the dashboard — and the mailbox history that
    was left unread is stated outright rather than quietly dropped."""
    from translog_quote.adapters.clock import FixedClock

    stale = ENQUIRY.model_copy(
        update={
            "message_id": "<ancient@client.example>",
            "received_at": ENQUIRY.received_at - timedelta(days=3),
        }
    )
    fresh_enquiry = ENQUIRY.model_copy(
        update={
            "message_id": "<demo-fresh@client.example>",
            "subject": "Air Freight Quote Demo",
            "received_at": ENQUIRY.received_at + timedelta(minutes=5),
        }
    )
    session = LiveSession(
        settings,
        source=GrowingSource((ENQUIRY,), (stale, ENQUIRY, fresh_enquiry)),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION, ENQUIRY_EXTRACTION),
        clock=FixedClock(ENQUIRY.received_at),
    )
    session.poll()
    session.approve_clarification(by=APPROVER)

    session.start_demonstration()
    session.poll()

    snap = live_serialize.snapshot(session)
    demo = snap["demonstration"]  # type: ignore[index]

    assert demo["active"] is True
    assert demo["following"] == 1
    assert demo["outside_messages"] == 1, "the three-day-old message was not read"
    ids = [row["request_id"] for row in snap["requests"]]  # type: ignore[index]
    assert len(ids) == 1, "the earlier request is not active work in this session"


def test_a_settled_request_leaves_the_dashboard(after_reply: LiveSession) -> None:
    """The desk shows work in play. Once the quotation has gone out there is
    nothing left for anybody to do with it, and a finished card sitting on the
    dashboard is the next demonstration's clutter."""
    request_id = next(iter(after_reply.requests))
    assert live_serialize.snapshot(after_reply)["requests"], "precondition: it is listed"

    after_reply.decide(request_id, choice="approve", by=APPROVER)

    assert live_serialize.snapshot(after_reply)["requests"] == []
    assert live_serialize.snapshot(after_reply)["demonstration"]["following"] == 0  # type: ignore[index]


def test_a_declined_request_leaves_the_dashboard_too(after_reply: LiveSession) -> None:
    """Settled is settled: a decline sends nothing and is equally finished."""
    request_id = next(iter(after_reply.requests))

    after_reply.decide(request_id, choice="decline", by=APPROVER, reason="not this time")

    assert live_serialize.snapshot(after_reply)["requests"] == []


def test_a_settled_request_is_still_readable_by_id(after_reply: LiveSession) -> None:
    """It leaves the desk, not the record. An operator reading the confirmation
    of the quotation they just approved must not have it vanish under them."""
    request_id = next(iter(after_reply.requests))
    after_reply.decide(request_id, choice="approve", by=APPROVER)

    detail = live_serialize.snapshot(after_reply, selected=request_id)["selected"]

    assert detail is not None
    assert detail["decision"]["sent"] is True  # type: ignore[index]
    assert detail["status"]["label"] == "QUOTATION SENT"  # type: ignore[index]


def test_an_unfinished_request_stays_on_the_dashboard(after_reply: LiveSession) -> None:
    """Only the two terminal states leave. A request waiting at the approval
    gate is exactly the work the desk exists to show."""
    assert len(live_serialize.snapshot(after_reply)["requests"]) == 1  # type: ignore[arg-type]


def test_a_manual_review_request_stays_on_the_dashboard(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """Handed to a person is not finished — it is the one state that most needs
    somebody to see it."""
    session = session_for(settings, sink)
    session.poll()
    request = only_request(session)
    request.state = RequestState.MANUAL_REVIEW  # type: ignore[attr-defined]

    assert len(live_serialize.snapshot(session)["requests"]) == 1  # type: ignore[arg-type]


def test_a_fresh_untouched_request_is_flagged_as_new(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    session = scoped_session(settings, sink, emails=(ENQUIRY,), extractions=(ENQUIRY_EXTRACTION,))
    session.start_demonstration()
    session.poll()

    assert live_serialize.snapshot(session)["requests"][0]["is_new"] is True  # type: ignore[index]


def test_the_new_badge_clears_once_the_request_is_under_way(
    settings: Settings, sink: CollectingEmailSink
) -> None:
    """Once a clarification has gone out the badge would be describing the past."""
    session = scoped_session(settings, sink, emails=(ENQUIRY,), extractions=(ENQUIRY_EXTRACTION,))
    session.start_demonstration()
    session.poll()

    session.approve_clarification(by=APPROVER)

    assert live_serialize.snapshot(session)["requests"][0]["is_new"] is False  # type: ignore[index]


def test_the_timeline_says_whose_move_it_is(settings: Settings, sink: CollectingEmailSink) -> None:
    """ "We are blocked on you" and "we are blocked on the client" are the two
    facts a presenter narrates, and one marker for both hides the difference."""
    session = session_for(settings, sink)
    session.poll()
    rows = {row["key"]: row for row in timeline_of(session)}
    assert rows["clarification_sent"]["waiting_on"] == "operator"

    session.approve_clarification(by=APPROVER)

    rows = {row["key"]: row for row in timeline_of(session)}
    assert rows["reply_received"]["state"] == "current"
    assert rows["reply_received"]["waiting_on"] == "client"
    assert rows["reply_received"]["note"] == "Waiting for client reply"


def test_a_completed_step_is_never_marked_as_waiting_on_anyone(
    after_reply: LiveSession,
) -> None:
    for row in timeline_of(after_reply):
        if row["state"] == "done":
            assert row["waiting_on"] is None


# --- the demonstration starts itself --------------------------------------------


def test_the_browser_cannot_start_a_demonstration(live_server: DemoServer) -> None:
    """The control is gone from the page and the endpoint behind it is gone
    from the server. A demonstration begins when the process does."""
    status, _ = call(live_server, "POST", "/api/live/demonstration/start", {})

    assert status == 404


# --- same-origin guard: CSRF and DNS rebinding ----------------------------------


def raw_post(server: DemoServer, path: str, *, headers: dict[str, str], body: bytes = b"{}") -> int:
    """A POST with fully controlled headers, for probing the request guard."""
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request("POST", path, body=body, headers=headers)
        return connection.getresponse().status
    finally:
        connection.close()


def test_a_cross_site_form_post_is_refused(live_server: DemoServer) -> None:
    """text/plain is the one body a cross-site HTML form can send without a
    CORS preflight. Refusing it turns away the CSRF shape that would otherwise
    let a page the operator visits forge an approval."""
    status = raw_post(
        live_server,
        "/api/live/poll",
        headers={"Host": "127.0.0.1", "Content-Type": "text/plain", "Content-Length": "2"},
    )

    assert status == 403


def test_a_form_encoded_post_is_refused(live_server: DemoServer) -> None:
    status = raw_post(
        live_server,
        "/api/live/quotation/decide",
        headers={
            "Host": "127.0.0.1",
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": "2",
        },
    )

    assert status == 403


def test_a_forged_host_is_refused(live_server: DemoServer) -> None:
    """DNS rebinding: an attacker domain resolved to 127.0.0.1 so its page can
    reach this server. The Host header still names the attacker."""
    status = raw_post(
        live_server,
        "/api/live/poll",
        headers={
            "Host": "attacker.example",
            "Content-Type": "application/json",
            "Content-Length": "2",
        },
    )

    assert status == 403


def test_a_forged_host_cannot_forge_an_approval(
    awaiting_server: tuple[DemoServer, CollectingEmailSink, str],
) -> None:
    """The guarantee that matters: a rebinding attempt cannot send a client
    email, because the request is refused before the gate is reached."""
    server, sink, request_id = awaiting_server

    status = raw_post(
        server,
        "/api/live/clarification/approve",
        headers={
            "Host": "attacker.example",
            "Content-Type": "application/json",
            "Content-Length": "40",
        },
        body=b'{"by":"attacker","request_id":"%b"}' % request_id.encode(),
    )

    assert status == 403
    assert sink.sent == [], "a forged-origin request must not send anything"


def test_a_same_origin_json_post_is_accepted(live_server: DemoServer) -> None:
    """The served page's own fetch sets application/json and a loopback Host,
    so the guard must never turn away a legitimate request."""
    status = raw_post(
        live_server,
        "/api/live/poll",
        headers={"Host": "127.0.0.1", "Content-Type": "application/json", "Content-Length": "2"},
    )

    assert status == 200


def test_a_loopback_host_with_a_port_is_accepted(live_server: DemoServer) -> None:
    """Browsers send Host as name:port. The port must not defeat the check."""
    status = raw_post(
        live_server,
        "/api/live/poll",
        headers={
            "Host": "localhost:8765",
            "Content-Type": "application/json",
            "Content-Length": "2",
        },
    )

    assert status == 200


def test_reads_are_not_gated_by_the_post_guard(live_server: DemoServer) -> None:
    """GET is safe by construction — it mutates nothing — so it stays reachable
    with whatever headers a browser sends."""
    status, _ = call(live_server, "GET", "/api/live/state")

    assert status == 200
