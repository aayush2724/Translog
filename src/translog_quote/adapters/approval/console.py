"""ConsoleApprovalGate — the approval gate as a terminal prompt.

The safest mechanism that fits this architecture, and the one this package has
described since Phase 1. The review packet is emailed to the internal approver
so a person reads it in their own mailbox; the *decision* is then typed at the
Translog terminal by whoever is running the system.

Why the decision is not taken by email reply: a ``From`` header is trivially
forged, so an inbound "APPROVE" is an instruction from an unauthenticated
sender. Accepting one would mean parsing commands out of untrusted mail and
wiring the read-only inbound path into the send path. The terminal has neither
problem — the person at the keyboard is the person running the process.

Three properties this class exists to hold:

- **No default.** Unrecognised input re-asks. It never falls through to either
  decision, and there is no timeout: an operator who walks away leaves the
  request at PENDING_APPROVAL, which is the correct outcome (BR-11).
- **No anonymous approval.** The approver's name is required and must be
  non-empty. "Who approved this" is not optional.
- **No approval by accident.** End-of-input raises rather than returning a
  decision. A closed pipe is not a person, and a ``Rejected`` value would be a
  false record of someone having declined.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, TextIO

from translog_quote.domain.quotation import Approved, Rejected
from translog_quote.errors import PermanentFailure

if TYPE_CHECKING:
    from collections.abc import Callable

    from translog_quote.domain.quotation import ApprovalDecision, ReviewPacket
    from translog_quote.ports import ClockPort

_APPROVE = {"approve", "a", "yes", "y"}
_DECLINE = {"decline", "d", "no", "n", "reject", "r"}

#: A stop, so a mistyped decision cannot loop forever against a stream that
#: will never produce a valid answer. Exhausting it raises; it never decides.
MAX_ATTEMPTS = 5


class _Abandoned(PermanentFailure):
    """No decision was given. Not a rejection — an absence of one.

    Modelled as a failure rather than as a ``Rejected`` because recording a
    decline nobody made would put a name in the audit trail that does not
    belong to a person who decided anything.
    """


class ConsoleApprovalGate:
    """An ``ApprovalPort`` that asks the operator at the terminal.

    ``approver`` may be supplied by the caller (from a command-line flag) or
    left unset, in which case the gate asks for it. Either way a name is
    recorded, and either way it is a name a person typed.
    """

    def __init__(
        self,
        *,
        clock: ClockPort,
        approver: str | None = None,
        read_line: Callable[[str], str] = input,
        out: TextIO = sys.stdout,
    ) -> None:
        self._clock = clock
        self._approver = (approver or "").strip() or None
        self._read_line = read_line
        self._out = out

    def request(self, review: ReviewPacket) -> ApprovalDecision:
        """Show the recommendation, then block until a person decides."""
        self._show(review)

        choice = self._ask_choice()
        who = self._ask_approver()

        if choice is True:
            return Approved(by=who, at=self._clock.now())
        return Rejected(by=who, at=self._clock.now(), reason=self._ask_reason())

    # -------------------------------------------------------------- prompts --

    def _show(self, review: ReviewPacket) -> None:
        """A summary at the terminal, so the decision is not taken blind even
        if the approver has not opened the emailed packet."""
        selection = review.selection
        rate = selection.rate
        record = review.record
        price = (
            f"{rate.total_amount} {rate.currency}"
            if rate.total_amount is not None and rate.currency is not None
            else "—"
        )
        rule = "=" * 66
        print(f"\n{rule}", file=self._out)
        print("  HUMAN APPROVAL REQUIRED — NOTHING HAS BEEN SENT TO THE CLIENT", file=self._out)
        print(rule, file=self._out)
        print(f"  request   : {review.request_id}", file=self._out)
        print(f"  shipment  : {record.origin} -> {record.destination}", file=self._out)
        print(
            f"  carrier   : {rate.carrier_name} ({rate.carrier_code}) {rate.product}",
            file=self._out,
        )
        print(f"  price     : {price}", file=self._out)
        print(f"  why       : {selection.reason}", file=self._out)
        print(f"  excluded  : {len(review.rates.excluded)} rate(s)", file=self._out)
        print(
            "\n  The full review packet has been emailed to the internal approver.",
            file=self._out,
        )
        print(
            "  Approving sends the quotation to the client. Declining sends nothing.",
            file=self._out,
        )
        print(rule, file=self._out)

    def _ask_choice(self) -> bool:
        """True to approve, False to decline. Never a default either way."""
        for _ in range(MAX_ATTEMPTS):
            answer = self._prompt("  Decision — type APPROVE or DECLINE: ").strip().lower()
            if answer in _APPROVE:
                return True
            if answer in _DECLINE:
                return False
            print(
                "  Not understood. Type APPROVE or DECLINE — there is no default.",
                file=self._out,
            )
        raise _Abandoned(
            "No approval decision was given. Nothing was sent; the request remains "
            "at PENDING_APPROVAL."
        )

    def _ask_approver(self) -> str:
        if self._approver is not None:
            return self._approver
        for _ in range(MAX_ATTEMPTS):
            who = self._prompt("  Your name, for the record: ").strip()
            if who:
                self._approver = who
                return who
            print("  A name is required. Decisions are never recorded anonymously.", file=self._out)
        raise _Abandoned("No approver was named. Nothing was sent.")

    def _ask_reason(self) -> str:
        """Optional, and only ever asked on the decline path.

        A blank reason is accepted: refusing to record a decline because the
        person did not want to explain it would be the wrong incentive.
        """
        try:
            return self._read_line("  Reason (optional, press Enter to skip): ").strip()
        except EOFError:
            return ""

    def _prompt(self, question: str) -> str:
        try:
            return self._read_line(question)
        except EOFError as exc:
            # A closed stream is not a person. Nothing here may decide.
            raise _Abandoned(
                "Input ended before a decision was given. Nothing was sent; the "
                "request remains at PENDING_APPROVAL."
            ) from exc
