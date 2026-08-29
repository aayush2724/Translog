"""The `gmail-quote` command, driven offline end to end.

The mailbox, the model and the terminal are stubbed. The router, correlation
policy, workflow, merge, validator, rate provider, filter, selection, approval
gate and state machine are all real — so what these tests exercise is the
actual safety behaviour, not a rehearsal of it.
"""

from __future__ import annotations

import io
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.test_gmail_thread import (
    ENQUIRY,
    ENQUIRY_EXTRACTION,
    ENQUIRY_ID,
    REPLY,
    REPLY_EXTRACTION,
    ScriptedExtractor,
    StubSource,
)

from translog_quote import bootstrap
from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.config import Settings
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractionResult
from translog_quote.domain.quotation import INTERNAL_SUBJECT_PREFIX, SIMULATED_RATE_NOTICE
from translog_quote.errors import PermanentFailure
from translog_quote.interface.demo import gmail_quote
from translog_quote.interface.demo.gmail_quote import (
    EXIT_AWAITING_APPROVAL,
    EXIT_CONFIG,
    EXIT_DECLINED,
    EXIT_GMAIL,
    EXIT_NO_DECISION,
    EXIT_NO_MESSAGE,
    EXIT_OK,
    run_gmail_quote,
)

FAKE_KEY = "test-not-a-real-credential"
APPROVER = "ops.manager@translog.example"
APPROVER_MAILBOX = "approvals@translog.example"
CLIENT = "client@example.com"


def settings_for(state_dir: Path | None = None) -> Settings:
    """Demo settings pointed at a throwaway state directory.

    `state_dir` defaults to a fresh temporary directory, never the configured
    `runs/state`: a test that persisted into the repository would leak state
    into the next test and into the developer's next real demo run. Tests that
    need two invocations to share state pass one explicitly.
    """
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    return base.model_copy(
        update={
            "demo": base.demo.model_copy(
                update={"state_dir": state_dir or Path(tempfile.mkdtemp())}
            ),
            "openrouter": base.openrouter.model_copy(update={"api_key": FAKE_KEY}),
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


class ScriptedTerminal:
    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)

    def __call__(self, prompt: str) -> str:
        if not self._answers:
            raise EOFError
        return self._answers.pop(0)


def wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    emails: tuple[RawEmail, ...] = (ENQUIRY, REPLY),
    terminal: ScriptedTerminal | None = None,
    extractions: tuple[ExtractionResult, ...] | None = None,
) -> CollectingEmailSink:
    """Replace the three real edges — mailbox, model, terminal — and nothing else.

    `extractions` scripts the model in the order the run will actually call it.
    It matters once persistence exists: a second invocation skips the enquiry,
    so its first extraction call is for the *reply*. Defaulting it to both
    results would quietly hand the reply the enquiry's fields and make a
    passing test describe something the demo never does.
    """
    sink = CollectingEmailSink()
    monkeypatch.setattr(bootstrap, "build_gmail_email_sink", lambda _s: sink)
    monkeypatch.setattr(bootstrap, "build_gmail_email_source", lambda _s, **_k: StubSource(*emails))
    script = extractions if extractions is not None else (ENQUIRY_EXTRACTION, REPLY_EXTRACTION)
    monkeypatch.setattr(bootstrap, "build_extractor", lambda _s: ScriptedExtractor(*script))
    monkeypatch.setattr(
        bootstrap,
        "build_console_approval",
        lambda **kwargs: _console(terminal or ScriptedTerminal(), kwargs.get("approver")),
    )
    return sink


def _console(terminal: ScriptedTerminal, approver: str | None) -> object:
    from translog_quote.adapters.approval import ConsoleApprovalGate

    return ConsoleApprovalGate(
        clock=bootstrap.build_fixed_clock(),
        approver=approver,
        read_line=terminal,
        out=io.StringIO(),
    )


def run(monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> tuple[int, str, CollectingEmailSink]:
    passthrough = {"emails", "terminal", "extractions"}
    sink = wire(monkeypatch, **{k: v for k, v in kwargs.items() if k in passthrough})
    out = io.StringIO()
    code = run_gmail_quote(
        settings=settings_for(),
        approved_by=kwargs.get("approved_by"),  # type: ignore[arg-type]
        cargo_is_liquid=kwargs.get("cargo_is_liquid"),  # type: ignore[arg-type]
        out=out,
    )
    return code, out.getvalue(), sink


def to_client(sink: CollectingEmailSink) -> list[object]:
    return [m for m in sink.sent if m.to_address == CLIENT]


def to_approver(sink: CollectingEmailSink) -> list[object]:
    return [m for m in sink.sent if m.to_address == APPROVER_MAILBOX]


# --- the approved path ----------------------------------------------------------


def test_an_approved_quotation_is_emailed_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, sink = run(monkeypatch, approved_by=APPROVER, terminal=ScriptedTerminal("approve"))

    assert code == EXIT_OK
    assert len(to_client(sink)) == 2  # the clarification, then the quotation
    assert "quotation sent   : yes" in out


def test_the_review_reaches_the_internal_approver_before_the_client_quotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, sink = run(monkeypatch, approved_by=APPROVER, terminal=ScriptedTerminal("approve"))

    addresses = [m.to_address for m in sink.sent]
    assert addresses.index(APPROVER_MAILBOX) < len(addresses) - 1
    assert addresses[-1] == CLIENT


def test_the_client_quotation_discloses_that_the_rates_are_simulated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DemoRateProvider's output must never reach a client dressed as a real
    market rate."""
    _, _, sink = run(monkeypatch, approved_by=APPROVER, terminal=ScriptedTerminal("approve"))

    quotation = to_client(sink)[-1]
    assert SIMULATED_RATE_NOTICE in quotation.body_text  # type: ignore[attr-defined]


def test_the_client_never_receives_the_internal_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, sink = run(monkeypatch, approved_by=APPROVER, terminal=ScriptedTerminal("approve"))

    for message in to_client(sink):
        assert INTERNAL_SUBJECT_PREFIX not in message.subject  # type: ignore[attr-defined]


# --- the declined path ----------------------------------------------------------


def test_a_decline_sends_no_quotation_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, sink = run(
        monkeypatch,
        approved_by=APPROVER,
        terminal=ScriptedTerminal("decline", "too expensive"),
    )

    assert code == EXIT_DECLINED
    assert len(to_client(sink)) == 1  # the clarification only
    assert len(to_approver(sink)) == 1
    assert "quotation sent   : no" in out
    assert "maker_rejected" in out


def test_a_decline_names_who_declined(monkeypatch: pytest.MonkeyPatch) -> None:
    _, out, _ = run(monkeypatch, approved_by=APPROVER, terminal=ScriptedTerminal("decline", ""))

    assert f"decided by       : {APPROVER}" in out


# --- no decision ----------------------------------------------------------------


def test_no_decision_sends_nothing_and_does_not_default_to_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator walked away. The request stays at PENDING_APPROVAL and the
    client hears nothing — there is no timeout into either outcome (BR-11)."""
    code, out, sink = run(
        monkeypatch,
        approved_by=APPROVER,
        terminal=ScriptedTerminal(),  # EOF immediately
    )

    assert code == EXIT_NO_DECISION
    assert len(to_client(sink)) == 1  # the clarification only
    assert "PENDING_APPROVAL" in out


# --- the clarification gate, unchanged ------------------------------------------


def test_without_a_named_approver_the_run_stops_before_the_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, sink = run(monkeypatch, approved_by=None)

    assert code == EXIT_AWAITING_APPROVAL
    assert sink.sent == []
    assert "--approved-by" in out


# --- configuration refusals -----------------------------------------------------


def test_the_run_refuses_when_outbound_gmail_is_disabled() -> None:
    """Off is the default. A token file lying around must not be enough."""
    base = settings_for()
    disabled = base.model_copy(
        update={"gmail": base.gmail.model_copy(update={"send_enabled": False})}
    )
    out = io.StringIO()

    assert run_gmail_quote(settings=disabled, out=out) == EXIT_CONFIG
    assert "outbound Gmail is disabled" in out.getvalue()


def test_the_run_refuses_without_an_internal_approver_address() -> None:
    base = settings_for()
    nowhere = base.model_copy(
        update={"gmail": base.gmail.model_copy(update={"approver_address": None})}
    )
    out = io.StringIO()

    assert run_gmail_quote(settings=nowhere, out=out) == EXIT_CONFIG
    assert "approver address" in out.getvalue()


def test_the_run_refuses_without_an_extraction_key() -> None:
    base = settings_for()
    keyless = base.model_copy(
        update={"openrouter": base.openrouter.model_copy(update={"api_key": None})}
    )
    out = io.StringIO()

    assert run_gmail_quote(settings=keyless, out=out) == EXIT_CONFIG


def test_a_broken_send_credential_stops_the_run_before_any_mail_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sink is built first on purpose: discovering a bad send credential
    after processing a client's email is a worse place to discover it."""
    reads: list[str] = []

    def refuse(_settings: Settings) -> object:
        raise PermanentFailure("no send token")

    monkeypatch.setattr(bootstrap, "build_gmail_email_sink", refuse)
    monkeypatch.setattr(
        bootstrap,
        "build_gmail_email_source",
        lambda _s, **_k: reads.append("read") or StubSource(ENQUIRY),  # type: ignore[func-returns-value]
    )
    out = io.StringIO()

    assert run_gmail_quote(settings=settings_for(), out=out) == EXIT_CONFIG
    assert reads == []


# --- mailbox handling -----------------------------------------------------------


def test_our_own_approval_request_is_never_ingested_as_a_client_enquiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The internal approver mailbox is the mailbox Translog reads. Without
    this filter the run would try to extract a shipment from its own review
    packet on the next pass."""
    review_mail = RawEmail(
        message_id="<review-1@translog.example>",
        from_address="translog@example.com",
        subject=f"{INTERNAL_SUBJECT_PREFIX} Quotation approval required — R-1 — A to B",
        body_text="QUOTATION AWAITING APPROVAL",
        received_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )
    code, out, _ = run(
        monkeypatch,
        emails=(ENQUIRY, review_mail, REPLY),
        approved_by=APPROVER,
        terminal=ScriptedTerminal("approve"),
    )

    assert code == EXIT_OK
    assert "1 internal approval mail(s) skipped" in out


def test_a_mailbox_holding_only_our_own_review_mail_processes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_only = RawEmail(
        message_id="<review-2@translog.example>",
        from_address="translog@example.com",
        subject=f"{INTERNAL_SUBJECT_PREFIX} Quotation approval required — R-2 — A to B",
        body_text="QUOTATION AWAITING APPROVAL",
        received_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )

    code, _, sink = run(monkeypatch, emails=(review_only,), approved_by=APPROVER)

    assert code == EXIT_NO_MESSAGE
    assert sink.sent == []


def test_an_empty_mailbox_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    code, _, sink = run(monkeypatch, emails=(), approved_by=APPROVER)

    assert code == EXIT_NO_MESSAGE
    assert sink.sent == []


def test_a_receive_failure_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = CollectingEmailSink()
    monkeypatch.setattr(bootstrap, "build_gmail_email_sink", lambda _s: sink)

    def refuse(_settings: Settings, **_kwargs: object) -> object:
        raise PermanentFailure("mailbox unavailable")

    monkeypatch.setattr(bootstrap, "build_gmail_email_source", refuse)
    out = io.StringIO()

    assert run_gmail_quote(settings=settings_for(), approved_by=APPROVER, out=out) == EXIT_GMAIL
    assert sink.sent == []


# --- the enquiry alone is not enough --------------------------------------------


def test_an_incomplete_shipment_is_never_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the enquiry, no reply. The shipment does not validate, so no rate
    search runs, the gate is never opened, and no quotation exists.

    What the client does receive is the clarification — which is the point of
    the run, not a failure of it. The assertion that matters is that the one
    message sent is the question, and no rate ever reached anybody.
    """
    code, out, sink = run(
        monkeypatch, emails=(ENQUIRY,), extractions=(ENQUIRY_EXTRACTION,), approved_by=APPROVER
    )

    assert code == EXIT_OK
    assert len(to_client(sink)) == 1
    assert to_approver(sink) == []  # the quotation gate was never reached
    assert SIMULATED_RATE_NOTICE not in to_client(sink)[0].body_text  # type: ignore[attr-defined]
    assert gmail_quote.DemoStatus.AWAITING_CLIENT_REPLY.value in out


def test_the_request_id_is_stable_across_runs() -> None:
    """A re-run addresses the same request rather than inventing a new one."""
    from translog_quote.interface.demo.gmail_thread import _request_id_for

    first = RawEmail(
        message_id=ENQUIRY_ID,
        from_address=CLIENT,
        subject="x",
        body_text="y",
        received_at=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(hours=1),
    )
    assert _request_id_for(first) == _request_id_for(ENQUIRY)
