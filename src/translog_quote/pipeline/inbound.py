"""Routing one inbound message to the request it belongs to.

    RawEmail -> CorrelationPolicy -> existing request_id | new request | refuse
             -> ClarificationWorkflow.handle
             -> record the message id on that request's thread

The step that was missing between "an email arrived" and "process it as request
R-123". Until now every caller supplied a ``request_id`` it had invented; this
module is where that identity is *decided*, once, by the policy — and where the
thread is recorded so the next reply can be placed against it.

It orchestrates and stores; it decides nothing. The correlation rule lives in
``domain.conversation``, the merge rule in ``domain.shipment``, and neither is
restated here. What this module owns is the consequence of a refusal: an
ambiguous message is not handed to the workflow at all, so there is no path by
which a message the policy could not place reaches a shipment record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from translog_quote.domain.conversation import AmbiguousCorrelation, NewRequest, Thread

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from translog_quote.domain.clarification import ClarificationMessage, UnresolvedPlace
    from translog_quote.domain.conversation import CorrelationPolicy
    from translog_quote.domain.email import RawEmail
    from translog_quote.domain.quotation import Approved
    from translog_quote.pipeline.clarification_loop import ClarificationWorkflow, TurnOutcome
    from translog_quote.ports import StorePort


@dataclass(frozen=True, slots=True)
class RoutedMessage:
    """What routing decided about one inbound message, and what came of it.

    ``outcome`` is ``None`` exactly when the message was refused: nothing was
    extracted, nothing was merged, and no request was touched.
    """

    request_id: str | None
    is_reply: bool
    """True when the policy placed this message on an *existing* request."""

    needs_manual_review: bool
    outcome: TurnOutcome | None = None
    reason: str = ""
    """Why a refused message was refused. Empty when it was routed."""

    ignored: bool = False
    """This message is not a Translog quotation request or a reply to one — an
    unrelated email (a newsletter, a receipt, an automated notification) that
    reached the mailbox. It was recorded as *seen* (so it is never re-examined)
    but produced no request, no extraction beyond recognition, no clarification,
    no rate search, and appears nowhere on the dashboard."""

    @property
    def was_refused(self) -> bool:
        return self.outcome is None


class InboundRouter:
    """Correlates inbound mail, then drives the existing clarification loop.

    ``new_request_id`` decides what a first-contact enquiry is called. It is a
    caller's concern, not a domain rule — a demo wants a deterministic id
    derived from the message, and production will want one from its own
    numbering — so it is injected rather than invented here.
    """

    def __init__(
        self,
        *,
        policy: CorrelationPolicy,
        workflow: ClarificationWorkflow,
        store: StorePort,
        new_request_id: Callable[[RawEmail], str],
    ) -> None:
        self._policy = policy
        self._workflow = workflow
        self._store = store
        self._new_request_id = new_request_id

    def route(self, email: RawEmail) -> RoutedMessage:
        """Place one message and process it. Never merges into a maybe."""
        decision = self._policy.correlate(email, self._store.all_threads())

        if isinstance(decision, AmbiguousCorrelation):
            # Deliberately not handed to the workflow. A message that cannot be
            # placed is not extracted, not merged, and not recorded against any
            # thread — recording it would be the same guess, one layer down.
            return RoutedMessage(
                request_id=None,
                is_reply=False,
                needs_manual_review=True,
                reason=(
                    "The reply's headers place it in more than one known request. "
                    "A person must decide which enquiry it answers."
                ),
            )

        is_reply = not isinstance(decision, NewRequest)

        # A NEW message from an automated/bulk sender is not a quotation enquiry
        # and cannot be a reply to one. Recognise it here, before the model is
        # ever called, so unrelated mail — a no-reply notification, a newsletter,
        # a receipt — creates no request, runs no extraction, writes no audit; it
        # is only recorded as *seen* so it is never re-examined. Replies are
        # exempt: a reply always belongs to a known thread and must continue.
        if not is_reply and _is_automated_sender(email):
            request_id = self._new_request_id(email)
            self._record(request_id, email.message_id)
            return RoutedMessage(
                request_id=request_id,
                is_reply=False,
                needs_manual_review=False,
                ignored=True,
                reason="Ignored: automated/bulk sender, not a quotation enquiry.",
            )

        request_id = self._new_request_id(email) if isinstance(decision, NewRequest) else decision

        outcome = self._workflow.handle(request_id, email)
        self._record(request_id, email.message_id)

        # A first-contact message that stated no shipment detail at all is
        # unrelated mail, recognised by content in the workflow. It is recorded
        # as seen (above) but produces no request and appears nowhere.
        if outcome.is_non_enquiry:
            return RoutedMessage(
                request_id=request_id,
                is_reply=is_reply,
                needs_manual_review=False,
                outcome=outcome,
                ignored=True,
                reason="Ignored: stated no shipment detail, not a quotation enquiry.",
            )

        return RoutedMessage(
            request_id=request_id,
            is_reply=is_reply,
            needs_manual_review=outcome.needs_a_person,
            outcome=outcome,
        )

    def already_processed(self, message_id: str) -> bool:
        """Whether this message is already recorded against some request.

        A read of the same threads correlation matches against, so "seen" means
        exactly what it means to the policy. Callers use it to skip a message a
        previous run already handled: re-handling one would call the model
        again, redraft a clarification that has already gone out, and — because
        the transition table forbids NEEDS_INFO -> EXTRACTED — could not
        legally advance the request anyway.

        It reports; it refuses nothing. `route` is unchanged, so a caller that
        does not ask still gets the previous behaviour.
        """
        return any(message_id in thread.message_ids for thread in self._store.all_threads())

    def pending_draft(self, request_id: str) -> ClarificationMessage | None:
        """The draft holding this request at NEEDS_INFO, if there is one."""
        return self._workflow.pending_draft(request_id)

    def request_location_clarification(
        self,
        request_id: str,
        unresolved: Sequence[UnresolvedPlace],
        *,
        to_address: str,
        subject: str,
        in_reply_to: str,
    ) -> ClarificationMessage | None:
        """Draft a location clarification for a request the rate-search step
        could not resolve. A pass-through, like `approve`: the router owns no
        decision here, it only saves the caller reaching into the workflow."""
        return self._workflow.request_location_clarification(
            request_id,
            unresolved,
            to_address=to_address,
            subject=subject,
            in_reply_to=in_reply_to,
        )

    def sweep_followup_deadlines(self) -> tuple[str, ...]:
        """Escalate any request whose 30-minute follow-up window has lapsed.

        A pass-through to the workflow, like the others: the router owns no
        decision here, it only saves the caller reaching into the workflow. The
        caller (the poll) uses the returned ids to mirror each hand-over into its
        own view and commit it durably."""
        return self._workflow.sweep_followup_deadlines()

    def approve(self, request_id: str, *, by: str) -> Approved:
        """Release a held draft on a named person's authority.

        A pass-through to the gate rather than a second gate: the router does
        not decide anything about approval, it only saves callers from reaching
        around it into the workflow. `by` stays required all the way down.
        """
        return self._workflow.approve_clarification(request_id, by=by)

    def _record(self, request_id: str, message_id: str) -> None:
        """Append this message to the request's thread.

        The thread is what the next reply correlates against, so it is written
        after the message has been processed rather than before: a message that
        failed to extract has not been seen, and should not silently become a
        correlation anchor.
        """
        existing = next((t for t in self._store.all_threads() if t.request_id == request_id), None)
        known = existing.message_ids if existing else ()
        if message_id in known:
            return  # the same message processed twice adds no new anchor
        self._store.save_thread(Thread(request_id=request_id, message_ids=(*known, message_id)))


#: Local parts that only ever belong to automated or bulk senders — never a
#: person sending a shipment enquiry. Deliberately conservative: a real client
#: address is never mistaken for one, and anything this misses is still caught by
#: the content check (a first-contact message stating no shipment is not an
#: enquiry). The recognition is by sender, not by subject, so an unusual subject
#: never hides a real enquiry and the word "quotation" never conjures one.
_AUTOMATED_LOCAL_PARTS = frozenset(
    {
        "no-reply", "noreply", "no_reply", "donotreply", "do-not-reply", "do_not_reply",
        "notify", "notification", "notifications", "alert", "alerts",
        "mailer", "mailer-daemon", "postmaster", "bounce", "bounces",
        "newsletter", "news", "updates",
    }
)

#: Substrings that mark an automated local part even inside a composite one
#: (e.g. "noreply.paytm", "paytm-notifications", "do-not-reply+promo").
_AUTOMATED_MARKERS = ("noreply", "donotreply", "notification", "mailerdaemon")


def _is_automated_sender(email: RawEmail) -> bool:
    """Whether the sender is an automated/bulk address that never sends a
    quotation enquiry. Matched on the address's local part, case-insensitively,
    both as a whole and with separators collapsed, so ``no-reply@…``,
    ``notifications@…`` and ``do-not-reply+x@…`` are recognised while an ordinary
    client address is not."""
    address = email.from_address.strip().lower()
    local = address.split("@", 1)[0] if "@" in address else address
    if local in _AUTOMATED_LOCAL_PARTS:
        return True
    collapsed = local.replace("-", "").replace("_", "").replace(".", "")
    return any(marker in collapsed for marker in _AUTOMATED_MARKERS)
