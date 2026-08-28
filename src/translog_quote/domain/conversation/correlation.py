"""The concrete correlation policy: RFC 5322 header chains, and nothing else.

    In-Reply-To  ->  the direct parent, per RFC 5322 §3.6.4
    References   ->  the ancestor chain, when In-Reply-To places nothing

Both are matched against ``Thread.message_ids`` — the message ids this system
has already seen for each request. That is the whole rule. It is deterministic,
provider-free, and testable without a mailbox.

What this policy deliberately refuses to do:

- **Subject matching.** In the reference thread the subject accumulated four
  concatenated titles across three days; two unrelated enquiries between the
  same two people share a subject constantly.
- **Sender-plus-recency guessing.** "The last thing this client wrote to us"
  is a heuristic, and a wrong merge silently corrupts a shipment record rather
  than failing loudly.
- **Provider thread ids.** Gmail's own threading groups by subject heuristics
  of its own, so a ``threadId`` is corroboration an adapter may record, never
  the business key. Nothing in this module knows what Gmail is.

When the headers place a message in more than one known request, the policy
returns ``AmbiguousCorrelation`` rather than picking. Merging into a maybe is
the one outcome that must never happen: a shipment assembled from two clients'
messages would still pass validation, and would be quoted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.domain.conversation.model import (
    AmbiguousCorrelation,
    CorrelationResult,
    NewRequest,
    Thread,
)

if TYPE_CHECKING:
    from translog_quote.domain.email import RawEmail


def _owners_of(message_ids: tuple[str, ...], threads: tuple[Thread, ...]) -> set[str]:
    """Which requests claim any of these message ids."""
    wanted = set(message_ids)
    return {thread.request_id for thread in threads if wanted.intersection(thread.message_ids)}


class HeaderChainCorrelation:
    """Matches a reply to its request by ``In-Reply-To``, then ``References``.

    The two headers are consulted in RFC precedence order, not merged into one
    bag of ids: ``In-Reply-To`` names the direct parent and is the stronger
    signal, so when it resolves to exactly one known request that request wins
    even if the wider ``References`` chain also touches another. A conversation
    that was forwarded between requests is exactly the case where pooling the
    two headers would manufacture a false ambiguity.
    """

    def correlate(self, email: RawEmail, threads: tuple[Thread, ...]) -> CorrelationResult:
        if not threads:
            return NewRequest()

        if email.in_reply_to:
            owners = _owners_of((email.in_reply_to,), threads)
            if len(owners) == 1:
                return owners.pop()
            if len(owners) > 1:
                # One message id claimed by two requests. Only reachable if the
                # store was written inconsistently, but "impossible" is not a
                # reason to merge into whichever one iterates first.
                return AmbiguousCorrelation()

        if email.references:
            owners = _owners_of(email.references, threads)
            if len(owners) == 1:
                return owners.pop()
            if len(owners) > 1:
                # The chain spans two known requests and In-Reply-To did not
                # settle it. A person decides which one this belongs to.
                return AmbiguousCorrelation()

        # Either no correlating headers at all (a fresh enquiry), or headers
        # whose ancestors we have never seen (a reply to something outside this
        # system). Both start a new request: nothing is merged into an existing
        # shipment, which is the safe outcome in both cases.
        return NewRequest()
