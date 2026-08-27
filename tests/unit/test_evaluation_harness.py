"""The evaluation harness's deterministic parts.

No PDFs, no network, no client data. These test the logic that decides who
wrote what and whether an extraction matched — the two places a bug would
quietly invalidate every accuracy number the harness reports.
"""

from __future__ import annotations

import pytest

from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.shipment import CargoDimensions
from translog_quote.evaluation.ground_truth import ExpectedCase, ExpectedField
from translog_quote.evaluation.pdf_text import normalise_whitespace, strip_page_furniture
from translog_quote.evaluation.scoring import Outcome, _norm_text, score_case, score_field
from translog_quote.evaluation.thread import (
    SenderKind,
    classify_sender,
    client_request_text,
    split_thread,
)

THREAD = """\
25/07/26, 9:08 PMTranslog Express Pvt Ltd Mail - Re: RATE FOR BAH
Page 1 of 2https://mail.google.com/mail/u/1/?ik=abc&view=pt
Arayna Shukla <arayna@translogexpress.com>
Re: RATE FOR BAH
2 messages
Prachi Shah <prachi@translogexpress.com> Wed, Jul 22, 2026 at 5:16 PM
To: buyer@clientco.example
Dear Sir, attached quotation. Gross weight 500 kgs, dimensions 24x34x6 inches.
On Wed, Jul 22, 2026 at 4:40 PM <buyer@clientco.example> wrote:
Please quote 250 kgs from Ahmedabad to Bahrain.
"""


# --- sender attribution -------------------------------------------------------


def test_translog_domain_is_the_only_thing_that_makes_a_message_ours() -> None:
    assert classify_sender("x@translogexpress.com") is SenderKind.TRANSLOG
    assert classify_sender("x@clientco.example") is SenderKind.CLIENT
    assert classify_sender("") is SenderKind.UNKNOWN


def test_a_lookalike_domain_is_not_translog() -> None:
    assert classify_sender("x@nottranslogexpress.com.evil.example") is SenderKind.CLIENT


def test_the_thread_splits_into_attributed_messages() -> None:
    messages = split_thread(normalise_whitespace(strip_page_furniture(THREAD)))

    assert len(messages) == 2
    assert {m.kind for m in messages} == {SenderKind.TRANSLOG, SenderKind.CLIENT}


def test_only_client_text_reaches_extraction() -> None:
    """The safety property the whole harness rests on. Translog restates weight
    and dimensions in its reply; scoring against those would measure nothing but
    our own words coming back."""
    text = client_request_text(split_thread(normalise_whitespace(strip_page_furniture(THREAD))))

    assert "250 kgs" in text  # the client's figure
    assert "500 kgs" not in text  # Translog's restatement
    assert "24x34x6" not in text  # Translog's restatement
    assert "attached quotation" not in text


def test_page_furniture_is_removed() -> None:
    cleaned = strip_page_furniture(THREAD)

    assert "mail.google.com" not in cleaned
    assert "Page 1 of 2" not in cleaned


def test_a_thread_with_no_boundaries_yields_nothing() -> None:
    """Better than treating an unrecognised file as one big client email."""
    assert split_thread("just some loose text\nwith no headers") == ()


# --- scoring ------------------------------------------------------------------


def _result(**kw: object) -> ExtractionResult:
    return ExtractionResult(**kw)  # type: ignore[arg-type]


def test_a_value_the_client_never_gave_is_a_hallucination() -> None:
    score = score_field(
        "origin",
        ExpectedField(status="not_stated"),  # type: ignore[arg-type]
        _result(origin=ExtractedValue[str].stated("Ahmedabad")),
    )

    assert score.outcome is Outcome.HALLUCINATED
    assert not score.is_pass


def test_a_value_the_client_gave_and_the_model_dropped_is_a_miss() -> None:
    score = score_field(
        "commodity",
        ExpectedField(status="stated", value="Pharma"),  # type: ignore[arg-type]
        _result(),
    )

    assert score.outcome is Outcome.MISSED


def test_two_different_stated_values_are_a_wrong_value() -> None:
    score = score_field(
        "commodity",
        ExpectedField(status="stated", value="Pharma"),  # type: ignore[arg-type]
        _result(commodity=ExtractedValue[str].stated("Machinery")),
    )

    assert score.outcome is Outcome.WRONG_VALUE


def test_free_text_specificity_passes_but_is_recorded_separately() -> None:
    """ "Bahrain" and "Bahrain (Hidd Industrial Area)" are the same destination
    at different lengths. Counted as a pass, flagged so the report shows how
    many passes leaned on the leniency."""
    score = score_field(
        "destination",
        ExpectedField(status="stated", value="Bahrain"),  # type: ignore[arg-type]
        _result(destination=ExtractedValue[str].stated("Bahrain (Hidd Industrial Area)")),
    )

    assert score.outcome is Outcome.CORRECT_CONTAINED
    assert score.is_pass


def test_numbers_are_not_leniently_matched() -> None:
    """500 kg is not 700 kg, however close the strings look."""
    score = score_field(
        "weight_kg",
        ExpectedField(status="stated", value=500),  # type: ignore[arg-type]
        _result(weight_kg=ExtractedValue[float].stated(700.0)),
    )

    assert score.outcome is Outcome.WRONG_VALUE


def test_dimensions_must_match_on_every_axis() -> None:
    expected = ExpectedField(status="stated", value={"length": 34, "width": 24, "height": 6})  # type: ignore[arg-type]

    same = score_field(
        "dimensions_in",
        expected,
        _result(
            dimensions_in=ExtractedValue[CargoDimensions].stated(
                CargoDimensions(length=34, width=24, height=6)
            )
        ),
    )
    swapped = score_field(
        "dimensions_in",
        expected,
        _result(
            dimensions_in=ExtractedValue[CargoDimensions].stated(
                CargoDimensions(length=24, width=34, height=6)
            )
        ),
    )

    assert same.outcome is Outcome.CORRECT
    assert swapped.outcome is Outcome.WRONG_VALUE


def test_agreeing_that_a_field_is_absent_is_correct() -> None:
    score = score_field("pcs", ExpectedField(status="not_stated"), _result())  # type: ignore[arg-type]

    assert score.outcome is Outcome.CORRECT


def test_expected_ambiguous_but_model_stated_is_a_status_mismatch() -> None:
    """A cm measurement confidently reported as inches."""
    score = score_field(
        "dimensions_in",
        ExpectedField(status="ambiguous", evidence="given in cm"),  # type: ignore[arg-type]
        _result(
            dimensions_in=ExtractedValue[CargoDimensions].stated(
                CargoDimensions(length=65, width=40, height=40)
            )
        ),
    )

    assert score.outcome is Outcome.STATUS_MISMATCH


def test_a_case_score_covers_every_canonical_field() -> None:
    case = ExpectedCase(case=1, fields={})
    score = score_case(case, _result())

    assert score.total == 11
    assert score.verdict == "PASS"  # empty expectations, empty result: all agree


def test_a_partially_correct_case_is_reported_as_partial() -> None:
    case = ExpectedCase(
        case=2,
        fields={"origin": ExpectedField(status="stated", value="Mumbai")},  # type: ignore[arg-type]
    )
    score = score_case(case, _result())

    assert score.verdict == "PARTIAL"
    assert score.passed == 10


@pytest.mark.parametrize("outcome", list(Outcome))
def test_only_the_two_correct_outcomes_count_as_passes(outcome: Outcome) -> None:
    expected = outcome in (Outcome.CORRECT, Outcome.CORRECT_CONTAINED)
    assert outcome.is_pass is expected


# --- dash normalisation in scoring ---------------------------------------------
#
# Added after the Phase 5.6 evaluation, where several fields were scored wrong
# purely because the model wrote an en dash where the ground truth had a hyphen.
# That measured the typography, not the extraction.


@pytest.mark.parametrize(
    ("dash", "name"),
    [
        ("‐", "hyphen"),
        ("‑", "non-breaking hyphen"),
        ("‒", "figure dash"),
        ("–", "en dash"),
        ("—", "em dash"),
        ("―", "horizontal bar"),
        ("−", "minus sign"),
    ],
)
def test_every_dash_variant_compares_equal_to_a_plain_hyphen(dash: str, name: str) -> None:
    score = score_field(
        "delivery_address",
        ExpectedField(status="stated", value="Houston, TX - 77041, USA"),  # type: ignore[arg-type]
        _result(delivery_address=ExtractedValue[str].stated(f"Houston, TX {dash} 77041, USA")),
    )

    assert score.outcome is Outcome.CORRECT, f"{name} not normalised"


def test_dashes_are_unified_not_removed() -> None:
    """The conservative half of the fix.

    Deleting dashes would make a range and a plain number compare equal, which
    is a genuinely different value and exactly the over-normalisation to avoid.
    """
    assert _norm_text("500-700") != _norm_text("500700")
    assert _norm_text("500–700") != _norm_text("500700")
    assert _norm_text("500-700") == _norm_text("500–700")


def test_normalisation_does_not_rescue_genuinely_different_values() -> None:
    """Both of these contain dashes and both must stay failures — a pickup
    address is not a city, however the punctuation is written."""
    for expected_value, actual_value in [
        ("Ahmedabad", "PLOT NO. 2317-2318-2329, - GIDC INDUSTRIAL ESTATE - METODA, RAJKOT"),
        ("Kathwada Singarva Road, Ahmedabad", "SHED NO : 01, VINAYAK INDUSTRIAL ESTATE-3"),
    ]:
        score = score_field(
            "origin",
            ExpectedField(status="stated", value=expected_value),  # type: ignore[arg-type]
            _result(origin=ExtractedValue[str].stated(actual_value)),
        )
        assert score.outcome is Outcome.WRONG_VALUE


def test_other_punctuation_is_still_significant() -> None:
    """Only dashes were normalised. Blanket punctuation stripping would start
    equating things that differ in meaning."""
    assert _norm_text("a/b") != _norm_text("ab")
    assert _norm_text("a:b") != _norm_text("ab")
    assert _norm_text("a|b") != _norm_text("ab")


def test_dash_normalisation_applies_to_the_expected_side_too() -> None:
    """Ground truth transcribed from a PDF can itself carry an en dash."""
    score = score_field(
        "destination",
        ExpectedField(status="stated", value="CVG AIRPORT – USA"),  # type: ignore[arg-type]
        _result(destination=ExtractedValue[str].stated("CVG AIRPORT - USA")),
    )

    assert score.outcome is Outcome.CORRECT
