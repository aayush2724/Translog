"""The header-chain correlation policy.

Pure domain: no mailbox, no store, no adapter. Each test states a header
situation and the request the policy is allowed to conclude from it.

The negative cases carry most of the weight. A wrong correlation is worse than
no correlation, because a reply merged into the wrong shipment produces a
record that validates — and then gets quoted.
"""

from __future__ import annotations

from datetime import UTC, datetime

from translog_quote.domain.conversation import (
    AmbiguousCorrelation,
    HeaderChainCorrelation,
    NewRequest,
    Thread,
)
from translog_quote.domain.email import RawEmail

POLICY = HeaderChainCorrelation()

ENQUIRY_ID = "<enquiry-1@mail.example.com>"
OTHER_ID = "<other-enquiry@mail.example.com>"

THREAD_A = Thread(request_id="R-A", message_ids=(ENQUIRY_ID,))
THREAD_B = Thread(request_id="R-B", message_ids=(OTHER_ID,))


def email(
    *,
    message_id: str = "<reply-1@mail.example.com>",
    in_reply_to: str | None = None,
    references: tuple[str, ...] = (),
    subject: str = "Re: Rate required - Ahmedabad to Bahrain",
    from_address: str = "client@example.com",
) -> RawEmail:
    return RawEmail(
        message_id=message_id,
        from_address=from_address,
        subject=subject,
        body_text="Commodity: Engineering components",
        received_at=datetime(2026, 9, 1, 14, 30, tzinfo=UTC),
        in_reply_to=in_reply_to,
        references=references,
    )


# --- correlating -----------------------------------------------------------------


def test_in_reply_to_pointing_at_a_known_message_correlates_to_its_request() -> None:
    result = POLICY.correlate(email(in_reply_to=ENQUIRY_ID), (THREAD_A,))

    assert result == "R-A"


def test_references_correlate_when_in_reply_to_is_absent() -> None:
    """Some clients send References without In-Reply-To."""
    result = POLICY.correlate(email(references=(ENQUIRY_ID,)), (THREAD_A,))

    assert result == "R-A"


def test_a_reference_chain_correlates_on_any_known_ancestor() -> None:
    chain = ("<root@elsewhere.example>", ENQUIRY_ID, "<mid@elsewhere.example>")

    assert POLICY.correlate(email(references=chain), (THREAD_A, THREAD_B)) == "R-A"


def test_in_reply_to_wins_over_a_references_chain_touching_another_request() -> None:
    """The direct parent is the stronger signal, per RFC 5322 §3.6.4. Pooling
    both headers would manufacture an ambiguity a forwarded chain creates."""
    result = POLICY.correlate(
        email(in_reply_to=ENQUIRY_ID, references=(OTHER_ID, ENQUIRY_ID)),
        (THREAD_A, THREAD_B),
    )

    assert result == "R-A"


def test_a_later_message_correlates_to_a_thread_with_several_known_ids() -> None:
    thread = Thread(request_id="R-A", message_ids=(ENQUIRY_ID, "<reply-1@mail.example.com>"))

    result = POLICY.correlate(email(in_reply_to="<reply-1@mail.example.com>"), (thread,))

    assert result == "R-A"


# --- negative case 1: unknown In-Reply-To ---------------------------------------


def test_a_reply_to_an_unknown_parent_starts_a_new_request() -> None:
    """Headers we cannot place must not merge into anything. Starting a new
    request is the outcome that touches no existing shipment."""
    result = POLICY.correlate(email(in_reply_to="<never-seen@elsewhere.example>"), (THREAD_A,))

    assert isinstance(result, NewRequest)


# --- negative case 2: no usable correlation headers -----------------------------


def test_a_message_with_no_correlation_headers_starts_a_new_request() -> None:
    result = POLICY.correlate(email(), (THREAD_A,))

    assert isinstance(result, NewRequest)


def test_the_first_message_ever_seen_starts_a_new_request() -> None:
    assert isinstance(POLICY.correlate(email(), ()), NewRequest)


def test_a_reply_arriving_before_any_thread_exists_starts_a_new_request() -> None:
    """No threads means nothing to correlate against, whatever the headers say."""
    assert isinstance(POLICY.correlate(email(in_reply_to=ENQUIRY_ID), ()), NewRequest)


# --- negative case 3: conflicting references ------------------------------------


def test_a_reference_chain_spanning_two_requests_is_ambiguous() -> None:
    result = POLICY.correlate(email(references=(ENQUIRY_ID, OTHER_ID)), (THREAD_A, THREAD_B))

    assert isinstance(result, AmbiguousCorrelation)


def test_an_ambiguous_chain_is_never_resolved_by_thread_ordering() -> None:
    """The same headers give the same refusal whichever order the store hands
    the threads back in — no iteration order can become a tiebreak."""
    references = (ENQUIRY_ID, OTHER_ID)

    forwards = POLICY.correlate(email(references=references), (THREAD_A, THREAD_B))
    backwards = POLICY.correlate(email(references=references), (THREAD_B, THREAD_A))

    assert isinstance(forwards, AmbiguousCorrelation)
    assert isinstance(backwards, AmbiguousCorrelation)


def test_one_message_id_claimed_by_two_requests_is_ambiguous() -> None:
    """Only reachable from an inconsistent store, but "impossible" is not a
    reason to merge into whichever thread iterates first."""
    duplicate = Thread(request_id="R-B", message_ids=(ENQUIRY_ID,))

    result = POLICY.correlate(email(in_reply_to=ENQUIRY_ID), (THREAD_A, duplicate))

    assert isinstance(result, AmbiguousCorrelation)


# --- negative case 4: a message belonging to another enquiry --------------------


def test_a_reply_to_another_enquiry_correlates_there_and_not_here() -> None:
    result = POLICY.correlate(email(in_reply_to=OTHER_ID), (THREAD_A, THREAD_B))

    assert result == "R-B"


# --- negative case 5: what must never influence the decision -------------------


def test_a_matching_subject_alone_never_correlates() -> None:
    """In the reference thread the subject accumulated four concatenated titles
    across three days. Subject is not identity."""
    same_subject = email(subject="Rate required - Ahmedabad to Bahrain")

    assert isinstance(POLICY.correlate(same_subject, (THREAD_A,)), NewRequest)


def test_the_same_sender_alone_never_correlates() -> None:
    thread_owner = email(from_address="client@example.com")

    assert isinstance(POLICY.correlate(thread_owner, (THREAD_A,)), NewRequest)


def test_a_body_that_looks_like_a_reply_never_correlates() -> None:
    quoted = email().model_copy(
        update={"body_text": "> Please provide a rate\nCommodity: Engineering components"}
    )

    assert isinstance(POLICY.correlate(quoted, (THREAD_A,)), NewRequest)


def test_correlation_reads_only_the_two_rfc_headers() -> None:
    """Changing everything except In-Reply-To/References cannot change the
    answer — the guard against a heuristic creeping in later."""
    baseline = POLICY.correlate(email(in_reply_to=ENQUIRY_ID), (THREAD_A,))
    altered = POLICY.correlate(
        email(
            in_reply_to=ENQUIRY_ID,
            message_id="<totally-different@x.example>",
            subject="Something else entirely",
            from_address="someone.else@example.com",
        ),
        (THREAD_A,),
    )

    assert baseline == altered == "R-A"
