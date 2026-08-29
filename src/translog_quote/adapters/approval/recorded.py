"""RecordedDecisionGate — the approval gate, decided through a user interface.

The console gate blocks on a prompt. This one blocks on a person clicking, and
the mechanism is the same halt: `QuotationStage` calls `request()` and does not
continue until a decision exists. What changed is where the person is sitting,
not whether one is required.

    console : print the packet -> block on input()      -> Approved | Rejected
    recorded: render the packet -> block on an HTTP POST -> Approved | Rejected

Three properties, each of which exists because the obvious implementation
would lose it:

- **The slot starts empty and consulting it empty raises.** A stage that
  somehow reached the gate without anyone having clicked fails loudly rather
  than falling through to either outcome. There is no default here any more
  than there is in the console gate.
- **A decision is single-use.** `request()` clears the slot, so a second
  `QuotationStage.run()` without a fresh click raises instead of silently
  re-applying the last person's answer to a different packet.
- **No decision can be constructed without a name.** That rule lives in
  `domain.quotation.decision_from_choice`, which is where "what counts as a
  decision" belongs — the same answer whether it arrives from a terminal or a
  browser. This class carries decisions; it does not define them.

Nothing here decides anything. It carries a decision made elsewhere, and
refuses to invent one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.errors import PermanentFailure

if TYPE_CHECKING:
    from translog_quote.domain.quotation import ApprovalDecision, ReviewPacket


class RecordedDecisionGate:
    """An `ApprovalPort` answering with the decision a person just made."""

    def __init__(self) -> None:
        self._pending: ApprovalDecision | None = None

    @property
    def has_decision(self) -> bool:
        """Whether a decision is waiting to be consumed. Read by callers that
        want to check before running a stage; never a substitute for one."""
        return self._pending is not None

    def record(self, decision: ApprovalDecision) -> None:
        """Place the decision a person has just made.

        Overwriting an unconsumed decision is refused. It would mean two
        decisions arrived for one packet and the second silently won, which is
        a race a person would never see and could not audit.
        """
        if self._pending is not None:
            raise PermanentFailure(
                "A decision is already waiting to be applied. Refusing to replace it."
            )
        self._pending = decision

    def request(self, review: ReviewPacket) -> ApprovalDecision:
        """The halt, resolved. Single-use, and never a default."""
        decision = self._pending
        self._pending = None
        if decision is None:
            raise PermanentFailure(
                "The approval gate was reached with no decision recorded. Nothing "
                "was sent; a person must explicitly approve or decline."
            )
        return decision
