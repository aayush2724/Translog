"""Pipeline (L3) — orchestration, state enforcement, audit emission.

Sequences the stages, drives the state machine and reports every step to the audit
trail. Depends on `domain` and `ports`; never on `adapters` or `interface`.

Phase 1 establishes the state machine and audit vocabulary. The stages themselves
arrive with the behaviour they orchestrate.
"""

from translog_quote.pipeline.state_machine import StateMachine

__all__ = ["StateMachine"]
