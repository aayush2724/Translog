"""The console approval gate. Everything here is about what it refuses to do.

The gate is the single control the whole outbound flow rests on, so these
tests are mostly negative: no default, no anonymous approval, no decision from
a closed stream, no approval from unrecognised input.
"""

from __future__ import annotations

import io

import pytest
from tests.unit.test_quotation_stage import packet_for

from translog_quote.adapters.approval import MAX_ATTEMPTS, ConsoleApprovalGate
from translog_quote.adapters.clock import FixedClock
from translog_quote.domain.quotation import Approved, Rejected
from translog_quote.errors import PermanentFailure

CLOCK = FixedClock()


class ScriptedInput:
    """Answers prompts from a fixed script; raises EOFError when exhausted."""

    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._answers:
            raise EOFError
        return self._answers.pop(0)


def gate(*answers: str, approver: str | None = None) -> tuple[ConsoleApprovalGate, io.StringIO]:
    out = io.StringIO()
    return (
        ConsoleApprovalGate(
            clock=CLOCK, approver=approver, read_line=ScriptedInput(*answers), out=out
        ),
        out,
    )


# --- the two decisions ----------------------------------------------------------


@pytest.mark.parametrize("answer", ["approve", "APPROVE", " Approve ", "y", "yes"])
def test_an_explicit_approval_is_recorded_with_the_approver(answer: str) -> None:
    approval, _ = gate(answer, "ops.manager@translog.example")

    decision = approval.request(packet_for())

    assert isinstance(decision, Approved)
    assert decision.by == "ops.manager@translog.example"
    assert decision.at == CLOCK.now()


@pytest.mark.parametrize("answer", ["decline", "DECLINE", "n", "no", "reject"])
def test_an_explicit_decline_is_recorded_as_a_rejection(answer: str) -> None:
    approval, _ = gate(answer, "ops.manager@translog.example", "price too high")

    decision = approval.request(packet_for())

    assert isinstance(decision, Rejected)
    assert decision.by == "ops.manager@translog.example"
    assert decision.reason == "price too high"


def test_a_decline_needs_no_reason() -> None:
    """A blank reason is accepted. Refusing to record a decline because the
    person did not want to explain it would be the wrong incentive."""
    approval, _ = gate("decline", "ops.manager@translog.example", "")

    decision = approval.request(packet_for())

    assert isinstance(decision, Rejected)
    assert decision.reason == ""


# --- what it will not do --------------------------------------------------------


def test_unrecognised_input_re_asks_rather_than_defaulting() -> None:
    """Neither "" nor "maybe" nor a stray keypress may become a decision."""
    approval, out = gate("", "maybe", "ok?", "approve", "ops.manager@translog.example")

    decision = approval.request(packet_for())

    assert isinstance(decision, Approved)
    assert out.getvalue().count("there is no default") == 3


def test_input_ending_before_a_decision_raises_rather_than_deciding() -> None:
    """A closed stream is not a person. In particular it is not a person who
    declined — recording a Rejected here would put a name in the audit trail
    belonging to nobody."""
    approval, _ = gate()  # no answers at all

    with pytest.raises(PermanentFailure, match="PENDING_APPROVAL"):
        approval.request(packet_for())


def test_persistently_unrecognised_input_gives_up_without_deciding() -> None:
    approval, _ = gate(*(["what?"] * (MAX_ATTEMPTS + 2)))

    with pytest.raises(PermanentFailure, match="No approval decision"):
        approval.request(packet_for())


def test_an_approval_cannot_be_recorded_anonymously() -> None:
    """ "Who approved this" is not optional. A blank name re-asks, and an
    exhausted stream raises rather than recording an empty approver."""
    approval, out = gate("approve", "", "   ")

    with pytest.raises(PermanentFailure):
        approval.request(packet_for())

    assert "never recorded anonymously" in out.getvalue()


def test_a_name_supplied_up_front_is_not_asked_for_again() -> None:
    """The operator named themselves on the command line. They still have to
    type the decision — the flag prefills who, never whether."""
    script = ScriptedInput("approve")
    out = io.StringIO()
    approval = ConsoleApprovalGate(
        clock=CLOCK, approver="ops.manager@translog.example", read_line=script, out=out
    )

    decision = approval.request(packet_for())

    assert isinstance(decision, Approved)
    assert decision.by == "ops.manager@translog.example"
    assert len(script.prompts) == 1
    assert "Decision" in script.prompts[0]


# --- what it shows before asking ------------------------------------------------


def test_the_prompt_states_that_nothing_has_been_sent_yet() -> None:
    approval, out = gate("approve", "ops.manager@translog.example")

    approval.request(packet_for())

    shown = out.getvalue()
    assert "NOTHING HAS BEEN SENT TO THE CLIENT" in shown
    assert "Declining sends nothing" in shown


def test_the_prompt_names_the_carrier_and_price_being_decided_on() -> None:
    """The decision is not taken blind even if the emailed packet went unread."""
    approval, out = gate("approve", "ops.manager@translog.example")

    approval.request(packet_for())

    shown = out.getvalue()
    assert "Emirates" in shown
    assert "EK" in shown
