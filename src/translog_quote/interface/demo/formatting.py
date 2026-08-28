"""Presentation for the extraction demo.

Pure functions over domain types: no I/O, no network, no settings. Every
function here takes a value and returns text, which is what makes the demo's
output testable without an API key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.domain.extraction import ExtractedValue, ExtractionResult, FieldStatus
from translog_quote.domain.shipment import CargoDimensions
from translog_quote.domain.validation import ValidationResult, ValidationSeverity

if TYPE_CHECKING:
    from translog_quote.domain.clarification import UnresolvedField
    from translog_quote.domain.email import RawEmail
    from translog_quote.domain.shipment import ShipmentRecord

RULE = "=" * 66
THIN = "-" * 66

_MAX_BODY_LINES = 22
_MAX_EVIDENCE_CHARS = 76

#: Display label per canonical field, in the order a reader wants them.
FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ("origin", "Origin"),
    ("destination", "Destination"),
    ("weight_kg", "Weight"),
    ("dimensions_in", "Dimensions"),
    ("commodity", "Commodity"),
    ("cargo_type", "Cargo Type"),
    ("is_chemical", "Chemical"),
    ("msds_attached", "MSDS"),
    ("pcs", "PCS"),
    ("delivery_type", "Delivery Type"),
    ("delivery_address", "Delivery Address"),
)

_LABEL_WIDTH = max(len(label) for _, label in FIELD_LABELS) + 1


def render_value(field: str, extracted: ExtractedValue[object]) -> str:
    """One field's value as a person would read it.

    The three non-``stated`` statuses render distinctly rather than all
    collapsing to a blank — showing the difference between "the email was
    silent" and "the client said no" is much of what this demo exists to prove.
    """
    if extracted.status is FieldStatus.NOT_STATED:
        return "— not stated"
    if extracted.status is FieldStatus.DENIED:
        return "— explicitly none"
    if extracted.status is FieldStatus.AMBIGUOUS:
        return f"— ambiguous ({extracted.note or 'no note'})"

    value = extracted.value
    if isinstance(value, CargoDimensions):
        return f"{value.length:g} (L) x {value.width:g} (W) x {value.height:g} (H) inches"
    if field == "weight_kg" and isinstance(value, int | float):
        return f"{value:g} kg"
    if field == "pcs":
        return f"{value} pieces"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def render_evidence(evidence: str) -> str:
    """Collapse a quote to one tidy line.

    Evidence is lifted verbatim from the email, so it can arrive with newlines
    and runs of spaces in it — a multi-line address, for instance. Left as-is it
    breaks the column alignment and makes the output harder to scan than the
    email it is quoting.
    """
    collapsed = " ".join(evidence.split())
    if len(collapsed) > _MAX_EVIDENCE_CHARS:
        collapsed = collapsed[: _MAX_EVIDENCE_CHARS - 1].rstrip() + "…"
    return collapsed


def render_transit(transit: object) -> str:
    """A transit time as a person writes it: "1 day", not "1 days".

    Shared so the rate demo and the quotation preview cannot drift apart on
    something a reader will notice immediately.
    """
    if transit is None:
        return "—"
    value = transit.value  # type: ignore[attr-defined]
    unit = transit.unit.value  # type: ignore[attr-defined]
    return f"{value} {unit.rstrip('s') if value == 1 else unit}"


def format_email(email: RawEmail) -> str:
    """The input, shown the way it arrived."""
    body_lines = email.body_text.strip().splitlines()
    shown = body_lines[:_MAX_BODY_LINES]
    truncated = len(body_lines) - len(shown)

    lines = [
        "EMAIL INPUT",
        THIN,
        f"Subject : {email.subject}",
        f"Client  : {email.from_address}",
        f"Received: {email.received_at:%Y-%m-%d %H:%M %z}",
        "",
        "Body:",
    ]
    lines.extend(f"  {line}" for line in shown)
    if truncated > 0:
        lines.append(f"  ... [{truncated} more line(s)]")
    return "\n".join(lines)


def format_extraction(result: ExtractionResult, *, show_evidence: bool = True) -> str:
    """What the model reported, field by field, with the text it grounded on.

    Evidence is the point of showing it: a value with a quote beside it can be
    checked against the email in a glance, which is the difference between
    trusting the extraction and verifying it.
    """
    lines = ["QWEN 3.7 FLASH EXTRACTION", THIN]

    for field, label in FIELD_LABELS:
        extracted: ExtractedValue[object] = getattr(result, field)
        lines.append(f"{(label + ':').ljust(_LABEL_WIDTH)} {render_value(field, extracted)}")
        if show_evidence and extracted.evidence:
            quote = render_evidence(extracted.evidence)
            lines.append(f'{" " * _LABEL_WIDTH}   evidence: "{quote}"')

    stated = len(result.fields_by_status(FieldStatus.STATED))
    lines.extend(["", f"{stated} of {len(FIELD_LABELS)} fields stated by the email."])
    return "\n".join(lines)


def format_record(record: ShipmentRecord) -> str:
    """The canonical record, rendered the way extraction fields are rendered.

    A known record value is by definition a stated one, so it is wrapped back
    into an `ExtractedValue` and handed to the shared renderer — same units,
    same labelled dimensions, one place to change the formatting.
    """
    width = _LABEL_WIDTH + 1
    lines = []
    for field, label in FIELD_LABELS:
        value = getattr(record, field)
        shown = (
            "— not known"
            if value is None
            else render_value(field, ExtractedValue[object].stated(value))
        )
        lines.append(f"{(label + ':').ljust(width)} {shown}")
    return "\n".join(lines)


def format_unresolved(unresolved: tuple[UnresolvedField, ...]) -> str:
    """What still needs the client's input, and why. Not the question text —
    that belongs to the draft, which is shown in full alongside it."""
    if not unresolved:
        return "  (nothing outstanding)"
    return "\n".join(f"  - {u.field.value}  [{u.reason.value}]" for u in unresolved)


def format_validation(validation: ValidationResult) -> str:
    """The deterministic verdict. Never the model's opinion."""
    status = "VALID" if validation.is_valid else "INVALID"
    lines = ["VALIDATION", THIN, f"Status: {status}"]

    if validation.is_valid:
        lines.append("All required shipment information is present.")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"{len(validation.issues)} issue(s) found:")
    for issue in validation.issues:
        marker = "!" if issue.severity is ValidationSeverity.INVALID else "?"
        lines.append(f"  [{marker}] {issue.rule_id.value}")
        lines.append(f"      {issue.message}")
    return "\n".join(lines)


def format_header(model: str) -> str:
    return "\n".join(
        [
            RULE,
            "TRANSLOG CARGO QUOTATION - AI EXTRACTION DEMO",
            RULE,
            f"Model: {model}   (live call via OpenRouter)",
        ]
    )


def format_footer(*, is_valid: bool) -> str:
    """What the pipeline would do next — stated, not performed."""
    next_step = (
        "Proceed to rate search (Phase 8+, not implemented)."
        if is_valid
        else "Send one batched clarification for the missing fields (Phase 6, not implemented)."
    )
    return "\n".join([THIN, f"Next step: {next_step}", RULE])
