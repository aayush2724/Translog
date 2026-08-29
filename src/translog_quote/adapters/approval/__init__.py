"""adapters.approval

Implements ApprovalPort. `ConsoleApprovalGate` asks the operator at the
terminal, after the review packet has been emailed to the internal approver.
It never returns a default: unrecognised input re-asks, an anonymous approval
is refused, and end-of-input raises rather than deciding (BR-11).
"""

from translog_quote.adapters.approval.console import MAX_ATTEMPTS, ConsoleApprovalGate

__all__ = ["MAX_ATTEMPTS", "ConsoleApprovalGate"]
