"""End-to-end audit of the whole workflow, on fresh data every time.

Written as a deliberate sweep rather than a bug-by-bug regression file: each
class below is one of the scenarios the operator asked to have proven, driven
through the real router, workflow, validator, merge, rate stage, state machine
and both approval gates. Only the mailbox, the model and the clock are stubbed.

Everything uses fresh message ids and fresh lanes, so no persisted state from
an earlier run can make a broken path look healthy.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.routing import StatedLocationResolver
from translog_quote.config import Settings
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.shipment import CargoDimensions, DeliveryType
from translog_quote.domain.workflow import RequestState
from translog_quote.errors import PermanentFailure
from translog_quote.interface.web.live_serialize import snapshot
from translog_quote.interface.web.live_session import LiveSession

APPROVER = "A. Operator"
BASE = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


# --- fixtures and helpers -------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    return base.model_copy(
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


class Mailbox:
    """A mailbox a test can add to between polls, as a real one changes."""

    def __init__(self, *emails: RawEmail) -> None:
        self._emails = list(emails)

    def add(self, email: RawEmail) -> None:
        self._emails.append(email)

    def fetch_new(self) -> tuple[RawEmail, ...]:
        return tuple(self._emails)


class KeyedExtractor:
    """Returns the extraction registered for the text it is given.

    Keyed rather than sequential because these tests poll repeatedly and in
    varying order; a scripted list would silently hand the wrong result to the
    wrong message and manufacture a passing test.
    """

    def __init__(self, by_marker: dict[str, ExtractionResult]) -> None:
        self._by_marker = by_marker
        self.calls: list[str] = []

    def extract_shipment(self, text: str) -> ExtractionResult:
        self.calls.append(text)
        for marker, result in self._by_marker.items():
            if marker in text:
                return result
        return ExtractionResult()

    def read_client_intent(self, text: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def enquiry(marker: str, client: str, minutes: int) -> RawEmail:
    return RawEmail(
        message_id=f"<enq-{marker}@mail.example.com>",
        from_address=client,
        subject=f"Air freight quote - {marker}",
        body_text=f"MARKER-{marker}. Please quote.",
        received_at=BASE + timedelta(minutes=minutes),
    )


def reply_to(parent: RawEmail, marker: str, minutes: int) -> RawEmail:
    return RawEmail(
        message_id=f"<rep-{marker}@mail.example.com>",
        from_address=parent.from_address,
        subject=f"Re: {parent.subject}",
        body_text=f"REPLY-{marker}. Details below.",
        received_at=BASE + timedelta(minutes=minutes),
        in_reply_to=parent.message_id,
        references=(parent.message_id,),
    )


def complete(origin: str, destination: str) -> ExtractionResult:
    return ExtractionResult(
        origin=ExtractedValue[str].stated(origin),
        destination=ExtractedValue[str].stated(destination),
        weight_kg=ExtractedValue[float].stated(500.0),
        dimensions_in=ExtractedValue[CargoDimensions].stated(
            CargoDimensions(length=34, width=24, height=6)
        ),
        commodity=ExtractedValue[str].stated("Machine parts"),
        cargo_type=ExtractedValue[str].stated("Non-Haz"),
        is_chemical=ExtractedValue[bool].stated(value=False),
        pcs=ExtractedValue[int].stated(8),
        delivery_type=ExtractedValue[DeliveryType].stated(DeliveryType.AIRPORT),
    )


def partial(origin: str, destination: str) -> ExtractionResult:
    """Everything except the delivery type, so exactly one field is missing."""
    result = complete(origin, destination)
    return result.model_copy(update={"delivery_type": ExtractedValue[DeliveryType].not_stated()})


def only_delivery() -> ExtractionResult:
    return ExtractionResult(delivery_type=ExtractedValue[DeliveryType].stated(DeliveryType.AIRPORT))


def build(settings: Settings, mailbox: Mailbox, extractor: object, sink: object) -> LiveSession:
    return LiveSession(
        settings,
        source=mailbox,  # type: ignore[arg-type]
        sink=sink,  # type: ignore[arg-type]
        extractor=extractor,  # type: ignore[arg-type]
        resolver=StatedLocationResolver(),
    )


def find(session: LiveSession, origin: str) -> object:
    matching = [r for r in session.requests.values() if r.record.origin == origin]
    assert len(matching) == 1, f"expected one request from {origin}, got {len(matching)}"
    return matching[0]


# --- A. the complete happy path -------------------------------------------------


def test_a_complete_enquiry_runs_to_quotation(settings: Settings) -> None:
    """Enquiry -> extraction -> validation -> rates -> gate -> quotation."""
    sink = CollectingEmailSink()
    mailbox = Mailbox(enquiry("KOCHI", "client-a@example.com", 0))
    session = build(settings, mailbox, KeyedExtractor({"KOCHI": complete("Kochi", "Dubai")}), sink)

    session.poll()
    request = find(session, "Kochi")

    assert request.state is RequestState.RATE_SELECTED  # type: ignore[attr-defined]
    assert request.clarification is None, "a complete enquiry asks nothing"  # type: ignore[attr-defined]
    assert request.rates is not None and request.rates.selection is not None  # type: ignore[attr-defined]
    assert sink.sent == [], "polling sends nothing"

    decided = session.decide(request.request_id, choice="approve", by=APPROVER)  # type: ignore[attr-defined]

    assert decided.state is RequestState.QUOTATION_SENT
    to = [m.to_address for m in sink.sent]
    assert to == ["approvals@translog.example", "client-a@example.com"]


# --- C. a reply from a different address ----------------------------------------


def test_a_reply_correlates_by_headers_not_by_sender(settings: Settings) -> None:
    """The client answers from a different address than they wrote from.

    Correlation is by RFC header chain, so this must still land on the right
    request — and must not be mistaken for a new enquiry.
    """
    sink = CollectingEmailSink()
    first = enquiry("DELHI", "buyer@example.com", 0)
    mailbox = Mailbox(first)
    session = build(
        settings,
        mailbox,
        KeyedExtractor({"DELHI": partial("Delhi", "Muscat"), "REPLY": only_delivery()}),
        sink,
    )
    session.poll()
    request = find(session, "Delhi")
    session.approve_clarification(by=APPROVER, request_id=request.request_id)  # type: ignore[attr-defined]

    colleague = reply_to(first, "REPLY", 30).model_copy(
        update={"from_address": "assistant@example.com"}
    )
    mailbox.add(colleague)
    session.poll()

    settled = find(session, "Delhi")
    assert settled.reply_received is True  # type: ignore[attr-defined]
    assert settled.state is RequestState.RATE_SELECTED  # type: ignore[attr-defined]
    assert len(session.requests) == 1, "the reply must not become a second request"


# --- D. several requests at once, replied to out of order -----------------------


@pytest.fixture
def three_open(settings: Settings) -> tuple[LiveSession, Mailbox, CollectingEmailSink, dict]:
    sink = CollectingEmailSink()
    a = enquiry("ALPHA", "alpha@example.com", 0)
    b = enquiry("BETA", "beta@example.com", 5)
    c = enquiry("GAMMA", "gamma@example.com", 10)
    mailbox = Mailbox(a, b, c)
    session = build(
        settings,
        mailbox,
        KeyedExtractor(
            {
                "ALPHA": partial("Ahmedabad", "Bahrain"),
                "BETA": partial("Bengaluru", "Dubai"),
                "GAMMA": partial("Chennai", "Singapore"),
                "RA": only_delivery(),
                "RB": only_delivery(),
                "RC": only_delivery(),
            }
        ),
        sink,
    )
    session.poll()
    return session, mailbox, sink, {"a": a, "b": b, "c": c}


def test_three_requests_stay_separate(
    three_open: tuple[LiveSession, Mailbox, CollectingEmailSink, dict],
) -> None:
    session, _, _, _ = three_open
    assert len(session.requests) == 3
    for origin in ("Ahmedabad", "Bengaluru", "Chennai"):
        assert find(session, origin).state is RequestState.NEEDS_INFO  # type: ignore[attr-defined]


def test_approving_one_mails_only_that_client(
    three_open: tuple[LiveSession, Mailbox, CollectingEmailSink, dict],
) -> None:
    """Approving B must never email client A."""
    session, _, sink, _ = three_open
    b = find(session, "Bengaluru")

    session.approve_clarification(by=APPROVER, request_id=b.request_id)  # type: ignore[attr-defined]

    assert [m.to_address for m in sink.sent] == ["beta@example.com"]
    assert find(session, "Ahmedabad").state is RequestState.NEEDS_INFO  # type: ignore[attr-defined]
    assert find(session, "Chennai").state is RequestState.NEEDS_INFO  # type: ignore[attr-defined]


def test_replies_arriving_out_of_order_land_on_their_own_requests(
    three_open: tuple[LiveSession, Mailbox, CollectingEmailSink, dict],
) -> None:
    """Approve A, B, C; then reply C, A, B. Each must update only itself."""
    session, mailbox, sink, mail = three_open
    for origin in ("Ahmedabad", "Bengaluru", "Chennai"):
        session.approve_clarification(by=APPROVER, request_id=find(session, origin).request_id)  # type: ignore[attr-defined]

    mailbox.add(reply_to(mail["c"], "RC", 40))
    session.poll()
    assert find(session, "Chennai").state is RequestState.RATE_SELECTED  # type: ignore[attr-defined]
    assert find(session, "Ahmedabad").state is RequestState.CLARIFICATION_SENT  # type: ignore[attr-defined]
    assert find(session, "Bengaluru").state is RequestState.CLARIFICATION_SENT  # type: ignore[attr-defined]

    mailbox.add(reply_to(mail["a"], "RA", 50))
    session.poll()
    assert find(session, "Ahmedabad").state is RequestState.RATE_SELECTED  # type: ignore[attr-defined]
    assert find(session, "Bengaluru").state is RequestState.CLARIFICATION_SENT  # type: ignore[attr-defined]

    mailbox.add(reply_to(mail["b"], "RB", 60))
    session.poll()
    assert find(session, "Bengaluru").state is RequestState.RATE_SELECTED  # type: ignore[attr-defined]

    lanes = {r.record.origin: r.record.destination for r in session.requests.values()}
    assert lanes == {"Ahmedabad": "Bahrain", "Bengaluru": "Dubai", "Chennai": "Singapore"}


def test_each_request_is_quoted_to_its_own_client(
    three_open: tuple[LiveSession, Mailbox, CollectingEmailSink, dict],
) -> None:
    session, mailbox, sink, mail = three_open
    for origin in ("Ahmedabad", "Bengaluru", "Chennai"):
        session.approve_clarification(by=APPROVER, request_id=find(session, origin).request_id)  # type: ignore[attr-defined]
    for key, marker in (("a", "RA"), ("b", "RB"), ("c", "RC")):
        mailbox.add(reply_to(mail[key], marker, 40))
    session.poll()

    for origin, client in (
        ("Ahmedabad", "alpha@example.com"),
        ("Bengaluru", "beta@example.com"),
        ("Chennai", "gamma@example.com"),
    ):
        session.decide(find(session, origin).request_id, choice="approve", by=APPROVER)  # type: ignore[attr-defined]
        quoted = [m for m in sink.sent if m.subject.startswith("Air freight quotation")]
        assert quoted[-1].to_address == client


# --- H. duplicate polling -------------------------------------------------------


def test_polling_repeatedly_changes_nothing(settings: Settings) -> None:
    """The same mail must not be extracted, merged or quoted twice."""
    sink = CollectingEmailSink()
    extractor = KeyedExtractor({"KOCHI": complete("Kochi", "Dubai")})
    mailbox = Mailbox(enquiry("KOCHI", "client@example.com", 0))
    session = build(settings, mailbox, extractor, sink)

    session.poll()
    calls_after_first = len(extractor.calls)
    rates_after_first = find(session, "Kochi").rates  # type: ignore[attr-defined]

    for _ in range(4):
        session.poll()

    assert len(session.requests) == 1
    assert len(extractor.calls) == calls_after_first, "no re-extraction"
    assert find(session, "Kochi").rates is rates_after_first, "no repeated rate search"  # type: ignore[attr-defined]
    assert sink.sent == []


def test_polling_after_a_quotation_does_not_resend_it(settings: Settings) -> None:
    sink = CollectingEmailSink()
    mailbox = Mailbox(enquiry("KOCHI", "client@example.com", 0))
    session = build(settings, mailbox, KeyedExtractor({"KOCHI": complete("Kochi", "Dubai")}), sink)
    session.poll()
    request = find(session, "Kochi")
    session.decide(request.request_id, choice="approve", by=APPROVER)  # type: ignore[attr-defined]
    sent_count = len(sink.sent)

    session.poll()
    session.poll()

    assert len(sink.sent) == sent_count, "a settled request must not be re-quoted"


def test_approving_the_same_clarification_twice_is_refused(settings: Settings) -> None:
    from translog_quote.interface.web.live_session import LiveSequenceError

    sink = CollectingEmailSink()
    mailbox = Mailbox(enquiry("DELHI", "client@example.com", 0))
    session = build(settings, mailbox, KeyedExtractor({"DELHI": partial("Delhi", "Muscat")}), sink)
    session.poll()
    request = find(session, "Delhi")
    session.approve_clarification(by=APPROVER, request_id=request.request_id)  # type: ignore[attr-defined]

    with pytest.raises(LiveSequenceError):
        session.approve_clarification(by=APPROVER, request_id=request.request_id)  # type: ignore[attr-defined]

    assert len(sink.sent) == 1, "exactly one clarification"


# --- J. a message from an unrelated conversation --------------------------------


def test_a_reply_to_an_unknown_thread_starts_its_own_request(settings: Settings) -> None:
    """It must not attach itself to whatever request happens to be open."""
    sink = CollectingEmailSink()
    open_one = enquiry("DELHI", "client@example.com", 0)
    mailbox = Mailbox(open_one)
    session = build(
        settings,
        mailbox,
        KeyedExtractor({"DELHI": partial("Delhi", "Muscat"), "STRAY": complete("Kochi", "Dubai")}),
        sink,
    )
    session.poll()

    stray = RawEmail(
        message_id="<stray@mail.example.com>",
        from_address="client@example.com",  # same client!
        subject="Re: something else entirely",
        body_text="MARKER-STRAY",
        received_at=BASE + timedelta(minutes=20),
        in_reply_to="<never-seen-by-us@elsewhere.example.com>",
        references=("<never-seen-by-us@elsewhere.example.com>",),
    )
    mailbox.add(stray)
    session.poll()

    assert len(session.requests) == 2, "an unknown chain is a new request, not a merge"
    assert find(session, "Delhi").state is RequestState.NEEDS_INFO  # type: ignore[attr-defined]
    assert find(session, "Delhi").reply_received is False  # type: ignore[attr-defined]


# --- F. the rate provider fails --------------------------------------------------


def test_a_rate_provider_failure_does_not_advance_or_corrupt(settings: Settings) -> None:
    """No false RATE_SELECTED, nothing sent, and the request stays retryable."""
    from translog_quote import bootstrap

    class Broken:
        adapter_id = "broken"

        def search(self, query: object) -> object:
            raise PermanentFailure("rate provider unavailable")

    sink = CollectingEmailSink()
    mailbox = Mailbox(enquiry("KOCHI", "client@example.com", 0))
    session = build(settings, mailbox, KeyedExtractor({"KOCHI": complete("Kochi", "Dubai")}), sink)

    original = bootstrap.build_demo_rate_provider
    bootstrap.build_demo_rate_provider = Broken  # type: ignore[assignment]
    try:
        session.poll()
    finally:
        bootstrap.build_demo_rate_provider = original  # type: ignore[assignment]

    request = find(session, "Kochi")
    assert request.state is RequestState.VALIDATED, "must not claim a rate was selected"  # type: ignore[attr-defined]
    assert request.rates is None  # type: ignore[attr-defined]
    assert request.packet is None, "no packet means the gate is unreachable"  # type: ignore[attr-defined]
    assert request.rate_failure is not None, "the failure must be visible"  # type: ignore[attr-defined]
    assert sink.sent == []

    # Retryable: the provider recovers and the next poll prices it.
    session.poll()
    healed = find(session, "Kochi")
    assert healed.state is RequestState.RATE_SELECTED  # type: ignore[attr-defined]
    assert healed.rate_failure is None  # type: ignore[attr-defined]


# --- G. reload / restart at each stage ------------------------------------------


@pytest.mark.parametrize("stop_after", ["needs_info", "clarification_sent", "quotation_sent"])
def test_state_survives_a_restart_at_each_stage(settings: Settings, stop_after: str) -> None:
    """A new process reading the same durable store must not lose the request.

    NEEDS_INFO is deliberately never persisted — an unapproved draft must not
    outlive the process that holds it — so that stage is asserted for what it
    actually guarantees: the mailbox is re-read and the draft rebuilt.
    """
    sink = CollectingEmailSink()
    first = enquiry("DELHI", "client@example.com", 0)
    mailbox = Mailbox(first)
    extractions = {"DELHI": partial("Delhi", "Muscat"), "REPLY": only_delivery()}
    session = build(settings, mailbox, KeyedExtractor(extractions), sink)
    session.poll()
    request_id = find(session, "Delhi").request_id  # type: ignore[attr-defined]

    if stop_after in ("clarification_sent", "quotation_sent"):
        session.approve_clarification(by=APPROVER, request_id=request_id)
    if stop_after == "quotation_sent":
        mailbox.add(reply_to(first, "REPLY", 30))
        session.poll()
        session.decide(request_id, choice="approve", by=APPROVER)

    # A brand-new session over the same durable store and the same mailbox.
    reborn = build(settings, mailbox, KeyedExtractor(extractions), CollectingEmailSink())
    reborn.poll()

    restored = reborn.requests.get(request_id)
    assert restored is not None, f"{stop_after} did not survive the restart"
    if stop_after == "quotation_sent":
        assert restored.state is RequestState.QUOTATION_SENT
    elif stop_after == "clarification_sent":
        assert restored.state in {
            RequestState.CLARIFICATION_SENT,
            RequestState.RATE_SELECTED,
        }
    else:
        assert restored.state is RequestState.NEEDS_INFO


# --- K. the UI payload agrees with the backend ----------------------------------


def test_the_serialised_state_matches_the_backend_at_each_stage(settings: Settings) -> None:
    """Whatever the backend says, the payload the browser renders must say."""
    sink = CollectingEmailSink()
    first = enquiry("DELHI", "client@example.com", 0)
    mailbox = Mailbox(first)
    session = build(
        settings,
        mailbox,
        KeyedExtractor({"DELHI": partial("Delhi", "Muscat"), "REPLY": only_delivery()}),
        sink,
    )

    session.poll()
    request_id = find(session, "Delhi").request_id  # type: ignore[attr-defined]

    def row() -> dict[str, object]:
        """The request as the dashboard lists it, while it is still work."""
        payload = snapshot(session, selected=request_id)
        return next(r for r in payload["requests"] if r["request_id"] == request_id)  # type: ignore[index,union-attr]

    def detail() -> dict[str, object]:
        """The request as the operator reads it, listed or not.

        A settled request leaves the dashboard — nothing further happens to it,
        and a finished card is the next demonstration's clutter — but it stays
        addressable, because the person who just approved a quotation is
        looking at the confirmation of what they sent.
        """
        payload = snapshot(session, selected=request_id)
        assert payload["selected"] is not None
        return payload["selected"]  # type: ignore[return-value]

    assert row()["status"]["state"] == "needs_info"  # type: ignore[index]
    assert row()["awaiting_clarification"] is True

    session.approve_clarification(by=APPROVER, request_id=request_id)
    assert row()["status"]["state"] == "clarification_sent"  # type: ignore[index]
    assert row()["awaiting_clarification"] is False

    mailbox.add(reply_to(first, "REPLY", 30))
    session.poll()
    assert row()["status"]["state"] == "rate_selected"  # type: ignore[index]
    assert row()["awaiting_decision"] is True

    session.decide(request_id, choice="approve", by=APPROVER)
    assert detail()["status"]["state"] == "quotation_sent"  # type: ignore[index]
    assert detail()["decision"]["sent"] is True  # type: ignore[index]
    assert snapshot(session)["requests"] == [], "and the desk is clear again"
