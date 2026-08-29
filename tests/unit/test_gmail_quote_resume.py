"""The demo across separate CLI invocations — the real-Gmail shape.

    run 1   enquiry arrives          -> clarification actually emailed
                                     -> persisted, process exits
    (the client replies, hours later)
    run 2   enquiry + reply in inbox -> enquiry skipped, reply processed
                                     -> rates, gate, quotation
    run 3   nothing new              -> no model call, no email

Each `run_gmail_quote` call here stands for a separate process: the durable
store is the only thing carried between them, exactly as it would be on the
day. The mailbox, the model and the terminal are stubbed; everything else —
correlation, merge, validation, the rate pipeline, both gates and the state
machine — is real.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from tests.unit.test_gmail_quote import (
    APPROVER,
    APPROVER_MAILBOX,
    CLIENT,
    ScriptedTerminal,
    settings_for,
    to_approver,
    to_client,
    wire,
)
from tests.unit.test_gmail_thread import (
    ENQUIRY,
    ENQUIRY_EXTRACTION,
    ENQUIRY_ID,
    REPLY,
    REPLY_EXTRACTION,
)

from translog_quote.adapters.store import REQUESTS_FILE, THREADS_FILE, JsonFileStore
from translog_quote.domain.quotation import SIMULATED_RATE_NOTICE
from translog_quote.domain.workflow import RequestState
from translog_quote.interface.demo.gmail_quote import (
    EXIT_AWAITING_APPROVAL,
    EXIT_OK,
    DemoStatus,
    run_gmail_quote,
)

REQUEST_ID = "R-GMAIL-4d4821069c"  # derived from ENQUIRY_ID; stable across runs


def invoke(
    monkeypatch: pytest.MonkeyPatch,
    state_dir: Path,
    *,
    emails: tuple[object, ...],
    extractions: tuple[object, ...],
    approved_by: str | None = APPROVER,
    terminal: ScriptedTerminal | None = None,
) -> tuple[int, str, object]:
    """One CLI invocation against a shared state directory."""
    sink = wire(
        monkeypatch,
        emails=emails,  # type: ignore[arg-type]
        extractions=extractions,  # type: ignore[arg-type]
        terminal=terminal,
    )
    out = io.StringIO()
    code = run_gmail_quote(settings=settings_for(state_dir), approved_by=approved_by, out=out)
    return code, out.getvalue(), sink


def stored(state_dir: Path) -> object:
    return JsonFileStore(state_dir).get_request(REQUEST_ID)


# --- run 1: the enquiry alone ---------------------------------------------------


def test_one_message_enquiry_actually_sends_the_clarification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The regression this whole change exists for.

    Before, the release only ran ahead of a *subsequent* message, so an enquiry
    sitting alone in the mailbox could never have its clarification sent — and
    the client could never produce the reply that would have triggered it.
    """
    code, out, sink = invoke(
        monkeypatch, tmp_path, emails=(ENQUIRY,), extractions=(ENQUIRY_EXTRACTION,)
    )

    assert code == EXIT_OK
    assert len(to_client(sink)) == 1
    assert to_client(sink)[0].subject.lower().startswith("re:")  # type: ignore[attr-defined]
    assert DemoStatus.AWAITING_CLIENT_REPLY.value in out


def test_the_clarification_state_is_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    invoke(monkeypatch, tmp_path, emails=(ENQUIRY,), extractions=(ENQUIRY_EXTRACTION,))

    request = stored(tmp_path)
    assert request is not None
    assert request.state is RequestState.CLARIFICATION_SENT  # type: ignore[attr-defined]


def test_the_persisted_state_is_readable_json_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """State a person can inspect between runs, not an opaque blob."""
    invoke(monkeypatch, tmp_path, emails=(ENQUIRY,), extractions=(ENQUIRY_EXTRACTION,))

    requests = json.loads((tmp_path / REQUESTS_FILE).read_text(encoding="utf-8"))
    threads = json.loads((tmp_path / THREADS_FILE).read_text(encoding="utf-8"))

    assert requests[REQUEST_ID]["state"] == "clarification_sent"
    assert threads[REQUEST_ID]["message_ids"] == [ENQUIRY_ID]


def test_a_run_that_stops_at_the_clarification_gate_persists_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing irreversible happened, so nothing is committed.

    This is not tidiness. The table permits no way out of NEEDS_INFO except
    CLARIFICATION_SENT, so persisting a merely-awaiting request would leave it
    unable to advance in any later process — one deadlock traded for another.
    """
    code, _, sink = invoke(
        monkeypatch,
        tmp_path,
        emails=(ENQUIRY,),
        extractions=(ENQUIRY_EXTRACTION,),
        approved_by=None,
    )

    assert code == EXIT_AWAITING_APPROVAL
    assert sink.sent == []  # type: ignore[attr-defined]
    assert stored(tmp_path) is None
    assert not (tmp_path / REQUESTS_FILE).exists()


def test_a_stopped_run_can_be_retried_and_then_sends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The dry run leaves the demo resumable rather than wedged."""
    invoke(
        monkeypatch,
        tmp_path,
        emails=(ENQUIRY,),
        extractions=(ENQUIRY_EXTRACTION,),
        approved_by=None,
    )

    code, _, sink = invoke(
        monkeypatch, tmp_path, emails=(ENQUIRY,), extractions=(ENQUIRY_EXTRACTION,)
    )

    assert code == EXIT_OK
    assert len(to_client(sink)) == 1


# --- run 2: the client's reply --------------------------------------------------


@pytest.fixture
def after_clarification(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """State as it stands once the clarification has really gone out."""
    invoke(monkeypatch, tmp_path, emails=(ENQUIRY,), extractions=(ENQUIRY_EXTRACTION,))
    return tmp_path


def test_the_second_invocation_does_not_resend_the_clarification(
    monkeypatch: pytest.MonkeyPatch, after_clarification: Path
) -> None:
    _, out, sink = invoke(
        monkeypatch,
        after_clarification,
        emails=(ENQUIRY, REPLY),
        extractions=(REPLY_EXTRACTION,),
        terminal=ScriptedTerminal("approve"),
    )

    subjects = [m.subject for m in to_client(sink)]  # type: ignore[attr-defined]
    assert not any(s.lower().startswith("re:") for s in subjects)
    assert "1 message(s) already processed" in out


def test_the_second_invocation_calls_the_model_only_for_the_new_reply(
    monkeypatch: pytest.MonkeyPatch, after_clarification: Path
) -> None:
    """A skipped message costs nothing. The extractor is scripted with exactly
    one result, so a second call would raise IndexError and fail this test."""
    code, _, _ = invoke(
        monkeypatch,
        after_clarification,
        emails=(ENQUIRY, REPLY),
        extractions=(REPLY_EXTRACTION,),
        terminal=ScriptedTerminal("approve"),
    )

    assert code == EXIT_OK


def test_the_second_invocation_correlates_and_merges_the_reply(
    monkeypatch: pytest.MonkeyPatch, after_clarification: Path
) -> None:
    """RFC header correlation still does the placing: the reply carries no
    origin, destination, weight or dimensions, and the quotation has all four."""
    _, out, sink = invoke(
        monkeypatch,
        after_clarification,
        emails=(ENQUIRY, REPLY),
        extractions=(REPLY_EXTRACTION,),
        terminal=ScriptedTerminal("approve"),
    )

    assert "REPLY -> correlated" in out
    quotation = to_client(sink)[-1]
    for carried in ("Ahmedabad", "Bahrain", "500 kg"):
        assert carried in quotation.body_text  # type: ignore[attr-defined]


def test_the_complete_flow_reaches_rate_selection(
    monkeypatch: pytest.MonkeyPatch, after_clarification: Path
) -> None:
    _, out, _ = invoke(
        monkeypatch,
        after_clarification,
        emails=(ENQUIRY, REPLY),
        extractions=(REPLY_EXTRACTION,),
        terminal=ScriptedTerminal("approve"),
    )

    assert "SELECTED — FASTEST ELIGIBLE" in out
    assert "SIMULATED" in out


def test_the_quotation_still_discloses_simulated_rates_across_runs(
    monkeypatch: pytest.MonkeyPatch, after_clarification: Path
) -> None:
    _, _, sink = invoke(
        monkeypatch,
        after_clarification,
        emails=(ENQUIRY, REPLY),
        extractions=(REPLY_EXTRACTION,),
        terminal=ScriptedTerminal("approve"),
    )

    assert SIMULATED_RATE_NOTICE in to_client(sink)[-1].body_text  # type: ignore[attr-defined]


# --- the gates survive persistence ----------------------------------------------


def test_approval_is_still_mandatory_after_a_resume(
    monkeypatch: pytest.MonkeyPatch, after_clarification: Path
) -> None:
    """Persistence must not become a way around the gate. The reply is merged
    and the rate selected, and still nothing reaches the client without a
    person typing a decision."""
    code, out, sink = invoke(
        monkeypatch,
        after_clarification,
        emails=(ENQUIRY, REPLY),
        extractions=(REPLY_EXTRACTION,),
        terminal=ScriptedTerminal(),  # EOF: the operator walked away
    )

    assert code != EXIT_OK
    assert to_client(sink) == []
    assert len(to_approver(sink)) == 1  # the review went out; the quotation did not
    assert "PENDING_APPROVAL" in out


def test_a_declined_quotation_is_never_sent_after_a_resume(
    monkeypatch: pytest.MonkeyPatch, after_clarification: Path
) -> None:
    _, _, sink = invoke(
        monkeypatch,
        after_clarification,
        emails=(ENQUIRY, REPLY),
        extractions=(REPLY_EXTRACTION,),
        terminal=ScriptedTerminal("decline", "price too high"),
    )

    assert to_client(sink) == []
    request = stored(after_clarification)
    assert request.state is RequestState.MAKER_REJECTED  # type: ignore[attr-defined]


def test_a_decline_is_not_reopened_by_a_later_run(
    monkeypatch: pytest.MonkeyPatch, after_clarification: Path
) -> None:
    """A terminal decision stays made. A later invocation must not offer the
    approver a second chance at it."""
    invoke(
        monkeypatch,
        after_clarification,
        emails=(ENQUIRY, REPLY),
        extractions=(REPLY_EXTRACTION,),
        terminal=ScriptedTerminal("decline", ""),
    )

    _, out, sink = invoke(
        monkeypatch,
        after_clarification,
        emails=(ENQUIRY, REPLY),
        extractions=(),
        terminal=ScriptedTerminal("approve"),
    )

    assert sink.sent == []  # type: ignore[attr-defined]
    assert "declined" in out.lower()


# --- run 3: nothing new ---------------------------------------------------------


@pytest.fixture
def after_quotation(monkeypatch: pytest.MonkeyPatch, after_clarification: Path) -> Path:
    invoke(
        monkeypatch,
        after_clarification,
        emails=(ENQUIRY, REPLY),
        extractions=(REPLY_EXTRACTION,),
        terminal=ScriptedTerminal("approve"),
    )
    return after_clarification


def test_a_third_invocation_sends_nothing_and_calls_no_model(
    monkeypatch: pytest.MonkeyPatch, after_quotation: Path
) -> None:
    """Duplicate final sending, prevented across separate processes.

    The in-memory ledger in `QuotationStage` is empty in this fresh run, so
    what stops the second send is the persisted state alone — which is the
    whole point of committing the decision before reporting it.
    """
    code, out, sink = invoke(
        monkeypatch,
        after_quotation,
        emails=(ENQUIRY, REPLY),
        extractions=(),  # a single model call would raise
        terminal=ScriptedTerminal("approve"),
    )

    assert code == EXIT_OK
    assert sink.sent == []  # type: ignore[attr-defined]
    assert DemoStatus.NOTHING_NEW.value in out
    assert "will not be sent again" in out


def test_the_quotation_state_survives_the_process(
    monkeypatch: pytest.MonkeyPatch, after_quotation: Path
) -> None:
    request = stored(after_quotation)

    assert request is not None
    assert request.state is RequestState.QUOTATION_SENT  # type: ignore[attr-defined]


def test_the_gate_refuses_a_request_a_previous_process_already_settled(
    monkeypatch: pytest.MonkeyPatch, after_quotation: Path
) -> None:
    """The guard directly, without relying on the command's message-skipping.

    Even handed the packet again with an empty in-memory ledger, the stage
    refuses on the persisted state and never consults a human.
    """
    from tests.unit.test_quotation_stage import StubApproval, approved, packet_for

    from translog_quote.errors import IllegalTransition

    approval = StubApproval(approved())
    store = JsonFileStore(after_quotation)
    stage = bootstrap_stage(store, approval)

    with pytest.raises(IllegalTransition, match="earlier run"):
        stage.run(
            packet_for().model_copy(update={"request_id": REQUEST_ID}),
            client_address=CLIENT,
            is_simulated=True,
        )

    assert approval.asked == 0


def bootstrap_stage(store: object, approval: object) -> object:
    from translog_quote.adapters.clock import FixedClock
    from translog_quote.adapters.email import CollectingEmailSink
    from translog_quote.pipeline import QuotationStage

    return QuotationStage(
        sink=CollectingEmailSink(),
        approval=approval,  # type: ignore[arg-type]
        clock=FixedClock(),
        approver_address=APPROVER_MAILBOX,
        store=store,  # type: ignore[arg-type]
    )
