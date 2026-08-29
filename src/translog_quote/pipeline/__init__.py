"""Pipeline (L3) — orchestration, state enforcement, audit emission.

Sequences the stages, drives the state machine and reports every step to the audit
trail. Depends on `domain` and `ports`; never on `adapters` or `interface`.

Holds the state machine, the audit vocabulary and the clarification loop. Later stages
arrive with the behaviour they orchestrate.
"""

from translog_quote.pipeline.clarification_loop import (
    ClarificationWorkflow,
    TurnOutcome,
)
from translog_quote.pipeline.inbound import InboundRouter, RoutedMessage
from translog_quote.pipeline.quotation import QuotationOutcome, QuotationStage
from translog_quote.pipeline.rate_search import (
    RateSearchOutcome,
    RateSearchStage,
    build_query,
)
from translog_quote.pipeline.state_machine import StateMachine

__all__ = [
    "ClarificationWorkflow",
    "InboundRouter",
    "QuotationOutcome",
    "QuotationStage",
    "RateSearchOutcome",
    "RateSearchStage",
    "RoutedMessage",
    "StateMachine",
    "TurnOutcome",
    "build_query",
]
