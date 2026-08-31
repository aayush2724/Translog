"""A reply's own words, separated from the thread quoted underneath it.

Every mail client appends the message being answered. Extraction runs on that
text, so a two-line reply arrives carrying the whole conversation — including
Translog's own clarification, which is not the client speaking.

The failure that produced this, reproduced from the live mailbox: a client
asked for "the package dimensions in inches (length x width x height)" replied

    Dimensions: 42 x 28 x 24 inches per package

and the model, reading our own quoted question beneath the answer, returned
`ambiguous` with the note "The email provides three dimensions but does not
label them as length, width, or height". Ambiguous maps to None in the
canonical record, so the merge had nothing to fill, the shipment stayed
incomplete, and the request never reached rate search. The same text with the
quote removed extracts cleanly — verified against the real message and the
real model, twice each way.

The stripper is deliberately conservative: only the two universal markers, and
never at the cost of losing the client's words.
"""

from __future__ import annotations

import pytest

from translog_quote.adapters.email.gmail import parse_gmail_message, strip_quoted_reply

#: The reply that exposed this, reduced to the shape that matters: a short
#: answer above a quoted clarification. Identities are fictional; the
#: structure — attribution line, `>` prefixes, our own question quoted
#: underneath the answer — is exactly what the live message carried.
REAL_REPLY = """Dear Translog Express Team,

Please find the requested details below:

Dimensions: 42 x 28 x 24 inches per package
Delivery type: Door delivery
Delivery address: 15 Example Street, Vienna, Austria

Please proceed with the quotation and provide the best available rate
and transit time.

Kind regards,
A. Client
Example Freight Ltd

On Mon, Aug 31, 2026 at 6:13 PM <translog@example.com> wrote:

> Thank you for your enquiry.
>
> To prepare an accurate quotation, could you please confirm the following:
>
>   1. The package dimensions in inches (length x width x height)
>   2. Whether you need door delivery or airport-to-airport
>
> Once we have these details we will come back to you with the rate.
>
> Kind regards,
> Translog Express
>
"""


# --- the reported case ----------------------------------------------------------


def test_the_clients_own_words_survive_and_the_quote_goes() -> None:
    stripped = strip_quoted_reply(REAL_REPLY)

    assert "Dimensions: 42 x 28 x 24 inches per package" in stripped
    assert "Delivery type: Door delivery" in stripped
    assert "15 Example Street, Vienna, Austria" in stripped
    assert "A. Client" in stripped


def test_our_own_clarification_is_not_left_in_the_text() -> None:
    """The specific contamination: the model read this and hedged."""
    stripped = strip_quoted_reply(REAL_REPLY)

    assert "length x width x height" not in stripped
    assert "Thank you for your enquiry" not in stripped
    assert "airport-to-airport" not in stripped
    assert ">" not in stripped


# --- shapes other clients produce -----------------------------------------------


@pytest.mark.parametrize(
    "attribution",
    [
        "On Mon, Aug 31, 2026 at 6:13 PM <a@b.com> wrote:",
        "On 31/08/2026 18:13, Translog Express <ops@translog.example> wrote:",
        "On Mon, 31 Aug 2026 at 18:13, someone very long indeed <x@y.example> wrote:",
    ],
)
def test_common_attribution_lines_end_the_message(attribution: str) -> None:
    text = f"Dimensions: 10 x 10 x 10 inches\n\n{attribution}\n> quoted question here\n"

    stripped = strip_quoted_reply(text)

    assert stripped == "Dimensions: 10 x 10 x 10 inches"


def test_quoted_lines_without_an_attribution_are_still_removed() -> None:
    text = "Delivery: door\n> we asked something\n>> and something older\n"

    assert strip_quoted_reply(text) == "Delivery: door"


# --- the conservative guarantees ------------------------------------------------


def test_a_message_with_no_quote_is_untouched() -> None:
    """Property 1: a first-contact enquiry must pass through unchanged."""
    enquiry = "500 KG, Ahmedabad to Bahrain, 24 x 34 x 6 inches, Non-Haz."

    assert strip_quoted_reply(enquiry) == enquiry


def test_a_reply_that_is_only_a_quote_keeps_its_text() -> None:
    """Never leave extraction with nothing. Reading too much beats reading none."""
    only_quote = "On Mon, Aug 31, 2026 at 6:13 PM <a@b.com> wrote:\n> the whole message\n"

    assert strip_quoted_reply(only_quote).strip() != ""


def test_the_word_wrote_inside_a_sentence_is_not_a_quote_marker() -> None:
    """`wrote:` must be a line of its own, not any use of the word."""
    text = "As I wrote: the dimensions are 42 x 28 x 24 inches.\nDelivery: door"

    stripped = strip_quoted_reply(text)

    assert "42 x 28 x 24" in stripped
    assert "Delivery: door" in stripped


def test_a_greater_than_sign_mid_line_is_not_a_quote() -> None:
    text = "Weight > 400 kg confirmed\nDimensions: 42 x 28 x 24 inches"

    stripped = strip_quoted_reply(text)

    assert "Weight > 400 kg confirmed" in stripped
    assert "42 x 28 x 24" in stripped


# --- it is applied where mail is parsed, not left to callers --------------------


def _gmail_message(body: str) -> dict[str, object]:
    import base64

    return {
        "id": "g1",
        "threadId": "t1",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "Message-ID", "value": "<m1@example.com>"},
                {"name": "From", "value": "client@example.com"},
                {"name": "Subject", "value": "Re: quote"},
                {"name": "Date", "value": "Mon, 31 Aug 2026 12:00:00 +0000"},
            ],
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
        },
    }


def test_parsing_a_gmail_reply_strips_the_quote() -> None:
    """Property 3: other fields in a reply keep working — they are in the same
    text, and the only thing removed is the part nobody sent."""
    parsed = parse_gmail_message(_gmail_message(REAL_REPLY))

    assert "42 x 28 x 24" in parsed.body_text
    assert "Delivery type: Door delivery" in parsed.body_text
    assert "length x width x height" not in parsed.body_text


def test_parsing_an_enquiry_leaves_it_alone() -> None:
    """Property 1 again, through the real parser."""
    enquiry = "Ahmedabad to Bahrain, 500 kg, 24 x 34 x 6 inches."

    assert parse_gmail_message(_gmail_message(enquiry)).body_text == enquiry
