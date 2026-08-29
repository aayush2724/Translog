"""adapters.approval

Implements ApprovalPort twice, for the two places a person may be sitting.

`ConsoleApprovalGate` asks the operator at the terminal. `RecordedDecisionGate`
satisfies the wider `DeferredApprovalPort`, carrying a decision made in a user
interface where the halt is an HTTP round trip rather than a blocking prompt.

Neither returns a default. Unrecognised input re-asks or raises, an anonymous
approval is refused, and a gate consulted with no decision recorded fails
rather than choosing an outcome (BR-11).
"""

from translog_quote.adapters.approval.console import MAX_ATTEMPTS, ConsoleApprovalGate
from translog_quote.adapters.approval.recorded import RecordedDecisionGate

__all__ = ["MAX_ATTEMPTS", "ConsoleApprovalGate", "RecordedDecisionGate"]
