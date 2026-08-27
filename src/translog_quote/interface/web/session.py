"""One demonstration run, held in memory, advanced only by explicit actions.

The session drives the real workflow the way the terminal POC does — the same
`ClarificationWorkflow`, the same `RateSearchStage`, the same approval gate —
but stops between steps and waits for a person to click. Each action method
guards its ordering: a reply cannot be processed before the clarification is
approved, rates cannot be searched before the shipment validates, and the
quotation approval records a simulated acknowledgement without constructing a
`Quotation`, because dispatch does not exist in this build and the type system
should keep saying so.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from translog_quote import bootstrap
from translog_quote.domain.quotation import Approved, ReviewPacket
from translog_quote.domain.rates import FASTEST_ELIGIBLE
from translog_quote.interface.web import scenario
from translog_quote.pipeline import RateSearchStage

if TYPE_CHECKING:
    from translog_quote.config import Settings
    from translog_quote.pipeline import RateSearchOutcome, TurnOutcome
    from translog_quote.ports import EmailSink

#: Who the demo records as the approver. A real deployment reads the signed-in
#: platform user; the web POC has no sign-in and says so in the name itself.
WEB_APPROVER = "demo.operator (web poc, simulated)"


class DemoStep(StrEnum):
    """Where the demonstration currently stands. Presentation vocabulary only —
    the workflow's own `RequestState` remains the authority on the request."""

    ENQUIRY_PROCESSED = "enquiry_processed"
    CLARIFICATION_APPROVED = "clarification_approved"
    REPLY_PROCESSED = "reply_processed"
    RATES_SEARCHED = "rates_searched"
    QUOTATION_ACKNOWLEDGED = "quotation_acknowledged"


class DemoSequenceError(Exception):
    """An action was requested out of order. A client error, not a system one."""


class DemoSession:
    """The scripted Northgate scenario, one real pipeline step per action."""

    def __init__(self, settings: Settings | None = None, *, sink: EmailSink | None = None) -> None:
        self._settings = settings or bootstrap.load_settings()
        self._clock = bootstrap.build_fixed_clock()
        self._workflow = bootstrap.build_clarification_workflow(
            self._settings,
            extractor=scenario.ScriptedExtractor(
                scenario.ENQUIRY_EXTRACTION, scenario.REPLY_EXTRACTION
            ),
            sink=sink,
        )

        self.step = DemoStep.ENQUIRY_PROCESSED
        #: The intake turn runs at construction: the dashboard's "INFORMATION
        #: REQUIRED" status is the real validator's verdict, not a hardcoded one.
        self.first: TurnOutcome = self._workflow.handle(
            scenario.REQUEST_ID, scenario.INITIAL_ENQUIRY
        )
        self.approval: Approved | None = None
        self.second: TurnOutcome | None = None
        self.rates: RateSearchOutcome | None = None
        self.packet: ReviewPacket | None = None
        self.quotation_acknowledgement: Approved | None = None

    # -------------------------------------------------------------- actions --

    def approve_clarification(self) -> None:
        """Release the pending draft through the real gate. Nothing is mailed:
        the sink collects in memory and no sender exists in this build."""
        if self.step is not DemoStep.ENQUIRY_PROCESSED:
            raise DemoSequenceError("no clarification draft is awaiting approval")
        self.approval = self._workflow.approve_clarification(scenario.REQUEST_ID, by=WEB_APPROVER)
        self.step = DemoStep.CLARIFICATION_APPROVED

    def receive_reply(self) -> None:
        """Process the simulated client reply through the real merge and
        revalidation. Only reachable after a person approved the draft."""
        if self.step is not DemoStep.CLARIFICATION_APPROVED:
            raise DemoSequenceError("the clarification has not been approved yet")
        self.second = self._workflow.handle(scenario.REQUEST_ID, scenario.CLIENT_REPLY)
        self.step = DemoStep.REPLY_PROCESSED

    def search_rates(self) -> None:
        """Run the real rate pipeline over the configured provider (the mock
        adapter, unless deployed otherwise) and build the review packet."""
        if self.step is not DemoStep.REPLY_PROCESSED or self.second is None:
            raise DemoSequenceError("there is no validated shipment to search rates for")
        if not self.second.is_complete:
            raise DemoSequenceError("the shipment did not validate; rates are not searched")

        stage = RateSearchStage(
            provider=bootstrap.build_rate_provider(self._settings), strategy=FASTEST_ELIGIBLE
        )
        self.rates = stage.run(
            scenario.REQUEST_ID,
            self.second.record,
            on_date=scenario.SEARCH_DATE,
            cargo_is_liquid=scenario.CARGO_IS_LIQUID,
        )
        if self.rates.selection is not None:
            self.packet = ReviewPacket(
                request_id=scenario.REQUEST_ID,
                record=self.second.record,
                validation=self.second.validation,
                clarification_sent=True,
                rates=self.rates.filtered,
                selection=self.rates.selection,
            )
        self.step = DemoStep.RATES_SEARCHED

    def acknowledge_quotation(self) -> None:
        """Record that the demo operator clicked approve. Simulated by design:
        no `Quotation` is constructed and nothing is dispatched — the send path
        does not exist in this build, and this method does not pretend it does."""
        if self.step is not DemoStep.RATES_SEARCHED or self.packet is None:
            raise DemoSequenceError("there is no quotation preview to approve")
        self.quotation_acknowledgement = Approved(by=WEB_APPROVER, at=self._clock.now())
        self.step = DemoStep.QUOTATION_ACKNOWLEDGED
