"""The whole proof of concept, in one run.

    client enquiry -> AI extraction -> validation -> clarification
                   -> simulated reply -> extraction -> merge -> revalidation
                   -> rate search -> eligibility -> fastest eligible rate
                   -> quotation preview

Real at every step except two, both stated in the output: the rates are mock
data, and nothing is sent anywhere. Extraction is a live model call; validation,
merging, clarification wording, filtering and selection are all deterministic.

Presentation only. Every decision is made by code that already exists and is
already tested — this module composes, and prints.
"""

from __future__ import annotations

import datetime
import sys
from typing import TYPE_CHECKING, TextIO

from translog_quote import bootstrap
from translog_quote.config import load_settings
from translog_quote.domain.email import RawEmail
from translog_quote.domain.quotation import ReviewPacket
from translog_quote.domain.rates import FASTEST_ELIGIBLE
from translog_quote.domain.shipment import CargoDimensions
from translog_quote.errors import TranslogError
from translog_quote.interface.demo.formatting import FIELD_LABELS, render_transit
from translog_quote.pipeline import RateSearchStage

if TYPE_CHECKING:
    from translog_quote.config import Settings
    from translog_quote.domain.shipment import ShipmentRecord
    from translog_quote.pipeline import RateSearchOutcome, TurnOutcome

REQUEST_ID = "R-POC-001"

RULE = "=" * 70
THIN = "-" * 70

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_EXTRACTION = 4
EXIT_STILL_INCOMPLETE = 5
EXIT_NO_RATE = 6

#: Fictional client and company. No real client correspondence appears here.
INITIAL_ENQUIRY = RawEmail(
    message_id="<enq-001@northgate-exports.example>",
    from_address="Priya Nair <priya.nair@northgate-exports.example>",
    subject="Rate required - Ahmedabad to Bahrain",
    body_text=(
        "Dear Sir/Madam,\n\n"
        "Please provide your best air freight rate for the shipment below.\n\n"
        "Origin: Ahmedabad\n"
        "Destination: Bahrain\n"
        "Gross weight: 500 KG\n"
        "Dimensions: 24 (width) x 34 (length) x 6 (breadth) inches\n"
        "Cargo type: Non-Haz\n\n"
        "Awaiting your rates.\n\n"
        "Regards,\n"
        "Priya Nair\n"
        "Northgate Exports\n"
    ),
    received_at=datetime.datetime(2026, 9, 1, 10, 15, tzinfo=datetime.UTC),
)

#: Supplies exactly what the first email left out — and nothing else, so the
#: merge has to preserve the origin, destination, weight and dimensions itself.
CLIENT_REPLY = RawEmail(
    message_id="<enq-002@northgate-exports.example>",
    from_address="Priya Nair <priya.nair@northgate-exports.example>",
    subject="Re: Rate required - Ahmedabad to Bahrain",
    body_text=(
        "Dear Sir/Madam,\n\n"
        "Please find the details below.\n\n"
        "Commodity: Engineering components\n"
        "Chemical: No\n"
        "Pieces: 10 cartons\n"
        "Delivery: Airport to airport\n\n"
        "Regards,\n"
        "Priya Nair\n"
    ),
    received_at=datetime.datetime(2026, 9, 1, 15, 40, tzinfo=datetime.UTC),
)

#: Stated by the demo, never derived. The canonical record cannot express
#: physical form, and guessing it from a commodity name would put a wrong
#: carrier on a real quotation (AMB-3). Engineering components are not liquid.
CARGO_IS_LIQUID = False

#: AMB-8: no approved source for a rate-search date exists, so the demo states
#: one rather than letting anything downstream invent it.
SEARCH_DATE = datetime.date(2026, 9, 2)


def _heading(number: int, title: str, out: TextIO, *, note: str = "") -> None:
    print(f"\n{number}. {title}", file=out)
    if note:
        print(f"   {note}", file=out)
    print(THIN, file=out)


def _shipment_table(record: ShipmentRecord, out: TextIO) -> None:
    width = max(len(label) for _, label in FIELD_LABELS) + 2
    for field, label in FIELD_LABELS:
        value = getattr(record, field)
        print(f"   {(label + ':').ljust(width)} {_render(field, value)}", file=out)


def _render(field: str, value: object) -> str:
    if value is None:
        return "— not yet known"
    if field == "weight_kg":
        return f"{value:g} kg"
    if field == "pcs":
        return f"{value} pieces"
    if isinstance(value, CargoDimensions):
        return f"{value.length:g} (L) x {value.width:g} (W) x {value.height:g} (H) inches"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _quotation_preview(packet: ReviewPacket, out: TextIO) -> None:
    """The preview. Every value comes from the record or the selected rate.

    Nothing is invented: no tax, surcharge, validity, insurance, handling
    charge, markup or payment term appears, because none is specified anywhere
    in this project. What is unavailable says so.
    """
    record, rate = packet.record, packet.selection.rate
    unavailable = "Not specified in POC"

    rows: list[tuple[str, str]] = [
        ("Status", "PREVIEW — not sent, not approved"),
        ("Reference", packet.request_id),
        ("", ""),
        ("Origin", record.origin or unavailable),
        ("Destination", record.destination or unavailable),
        ("Commodity", record.commodity or unavailable),
        ("Cargo type", record.cargo_type or unavailable),
        ("Chemical", "Yes" if record.is_chemical else "No"),
        ("Weight", f"{record.weight_kg:g} kg" if record.weight_kg else unavailable),
        ("Dimensions", _render("dimensions_in", record.dimensions_in)),
        ("Pieces", f"{record.pcs} pieces" if record.pcs else unavailable),
        (
            "Delivery",
            record.delivery_type.value if record.delivery_type else unavailable,
        ),
        ("Delivery address", record.delivery_address or unavailable),
        ("", ""),
        ("Carrier", f"{rate.carrier_name} ({rate.carrier_code})"),
        ("Service", rate.product),
        ("Transit time", render_transit(rate.transit)),
        ("Air freight", f"{rate.total_amount} {rate.currency}"),
        ("", ""),
        ("Taxes and surcharges", unavailable),
        ("Validity", unavailable),
        ("Payment terms", unavailable),
    ]

    width = max(len(label) for label, _ in rows) + 2
    for label, value in rows:
        print("" if not label else f"   {(label + ':').ljust(width)} {value}", file=out)


def run_demo(*, settings: Settings | None = None, out: TextIO = sys.stdout) -> int:
    settings = settings or load_settings()

    print(RULE, file=out)
    print("TRANSLOG POC — END-TO-END QUOTATION DEMO", file=out)
    print(RULE, file=out)
    print("  AI extraction     LIVE   " + settings.openrouter.model, file=out)
    print("  Validation        REAL   deterministic rules", file=out)
    print("  Clarification     REAL   deterministic wording", file=out)
    print("  Merge             REAL   deterministic", file=out)
    print("  Rate data         MOCK   no WebCargo request is made", file=out)
    print("  Email sending     NONE   nothing leaves this process", file=out)

    if settings.openrouter.api_key is None:
        print("\n  Cannot run: no OpenRouter API key configured.", file=out)
        print("  Set TRANSLOG_OPENROUTER__API_KEY in .env (see .env.example).\n", file=out)
        return EXIT_CONFIG

    workflow = bootstrap.build_clarification_workflow(settings)

    # --- 1-4. enquiry, extraction, validation, clarification ------------------
    _heading(1, "CLIENT ENQUIRY", out, note="(simulated inbound email)")
    print(f"   From:    {INITIAL_ENQUIRY.from_address}", file=out)
    print(f"   Subject: {INITIAL_ENQUIRY.subject}\n", file=out)
    for line in INITIAL_ENQUIRY.body_text.strip().splitlines():
        print(f"   | {line}", file=out)

    try:
        first: TurnOutcome = workflow.handle(REQUEST_ID, INITIAL_ENQUIRY)
    except TranslogError as exc:
        print(f"\n   EXTRACTION FAILED: {type(exc).__name__}\n   {exc}\n", file=out)
        return EXIT_EXTRACTION

    _heading(2, "AI EXTRACTION", out, note=f"(live — {settings.openrouter.model})")
    _shipment_table(first.record, out)

    _heading(3, "VALIDATION", out)
    print(f"   Result: {'VALID' if first.validation.is_valid else 'INCOMPLETE'}", file=out)

    if first.clarification is None:
        print("\n   Nothing to clarify — this demo needs an incomplete enquiry.\n", file=out)
        return EXIT_STILL_INCOMPLETE

    _heading(4, "CLARIFICATION REQUIRED", out, note="(generated, not sent)")
    for i, item in enumerate(first.clarification.unresolved, start=1):
        print(f"   {i}. {item.question}", file=out)
    print("\n   CLARIFICATION SENT  — simulated; no email left this process", file=out)
    print(f"   {THIN}", file=out)
    for line in first.clarification.body_text.splitlines():
        print(f"   | {line}", file=out)

    # --- 5-6. reply, merge, revalidation ---------------------------------------
    _heading(5, "CLIENT REPLY", out, note="(simulated demonstration input)")
    for line in CLIENT_REPLY.body_text.strip().splitlines():
        print(f"   | {line}", file=out)

    try:
        second: TurnOutcome = workflow.handle(REQUEST_ID, CLIENT_REPLY)
    except TranslogError as exc:
        print(f"\n   EXTRACTION FAILED: {type(exc).__name__}\n   {exc}\n", file=out)
        return EXIT_EXTRACTION

    _heading(6, "UPDATED SHIPMENT", out, note="(initial enquiry + reply, merged)")
    _shipment_table(second.record, out)
    carried = [f for f in ("origin", "destination", "weight_kg") if getattr(second.record, f)]
    print(f"\n   carried over from the enquiry: {', '.join(carried)}", file=out)
    print(
        f"   supplied by the reply:        {', '.join(f.value for f in second.merge.changed)}",
        file=out,
    )
    print(f"\n   VALIDATION: {'PASS' if second.validation.is_valid else 'FAIL'}", file=out)
    print(f"   STATUS:     {second.state.value.upper()}", file=out)

    if not second.is_complete:
        print("\n   Still incomplete after clarification. Stopping — a quotation", file=out)
        print("   will not be produced from an incomplete shipment.", file=out)
        for issue in second.validation.issues:
            print(f"     - {issue.field.value}: {issue.message}", file=out)
        print(RULE, file=out)
        return EXIT_STILL_INCOMPLETE

    # --- 7-8. rate search and selection ----------------------------------------
    stage = RateSearchStage(
        provider=bootstrap.build_rate_provider(settings), strategy=FASTEST_ELIGIBLE
    )
    rates: RateSearchOutcome = stage.run(
        REQUEST_ID, second.record, on_date=SEARCH_DATE, cargo_is_liquid=CARGO_IS_LIQUID
    )

    label = (
        "MOCK WEBCARGO DATA — POC ONLY. No WebCargo request was made."
        if rates.uses_mock_data
        else f"PROVIDER DATA — {rates.adapter_id}"
    )
    _heading(7, "RATE SEARCH", out, note=label)
    print(
        f"   Query: {rates.query.origin_iata} -> {rates.query.destination_iata}   "
        f"{rates.query.weight_kg:g} kg   on {rates.query.date}",
        file=out,
    )
    print(f"\n   Rates returned: {rates.returned}\n", file=out)
    print(f"   {'':<5}{'CARRIER':<22}{'PRICE':>16}{'TRANSIT':>12}", file=out)
    for r in rates.filtered.eligible:
        transit = render_transit(r.transit)
        price = f"{r.total_amount} {r.currency}"
        print(
            f"   {r.carrier_code:<5}{r.carrier_name:<22}{price:>16}{transit:>12}",
            file=out,
        )

    print(
        f"\n   Eligible: {len(rates.filtered.eligible)}    "
        f"Rejected: {len(rates.filtered.excluded)}",
        file=out,
    )
    for e in rates.filtered.excluded:
        print(f"     - {e.rate.carrier_name}: {e.detail}", file=out)

    if rates.selection is None:
        _heading(8, "NO ELIGIBLE RATE", out)
        print("   No rate satisfied the eligibility rules. Stopping before", file=out)
        print("   quotation preview rather than producing a misleading one.", file=out)
        print(RULE, file=out)
        return EXIT_NO_RATE

    chosen = rates.selection.rate
    _heading(8, "FASTEST ELIGIBLE RATE", out)
    print(f"   Carrier:  {chosen.carrier_name} ({chosen.carrier_code})", file=out)
    print(f"   Service:  {chosen.product}", file=out)
    print(f"   Transit:  {render_transit(chosen.transit)}", file=out)
    print(f"   Price:    {chosen.total_amount} {chosen.currency}", file=out)
    print(f"   Reason:   {rates.selection.reason}", file=out)
    if rates.selection.runners_up:
        print("\n   Ranked by transit time, not price:", file=out)
        for r in (chosen, *rates.selection.runners_up):
            t = render_transit(r.transit)
            marker = "->" if r is chosen else "  "
            print(
                f"   {marker} {t:<8} {r.carrier_name:<22} {r.total_amount} {r.currency}", file=out
            )

    # --- 9-10. quotation preview -----------------------------------------------
    packet = ReviewPacket(
        request_id=REQUEST_ID,
        record=second.record,
        validation=second.validation,
        clarification_sent=True,
        rates=rates.filtered,
        selection=rates.selection,
    )

    _heading(9, "QUOTATION PREVIEW", out, note="POC QUOTATION PREVIEW — MOCK RATE DATA")
    _quotation_preview(packet, out)
    print("\n   [ APPROVE QUOTATION ]   <- not wired. A quotation is only sent", file=out)
    print("                            after a quotation maker approves it,", file=out)
    print("                            and that gate is not built yet.", file=out)

    _heading(10, "DEMO COMPLETE", out)
    print("   Extraction        live model", file=out)
    print("   Validation        real, deterministic", file=out)
    print("   Clarification     real, deterministic", file=out)
    print("   Merge             real, deterministic", file=out)
    print("   Rate data         MOCK — not WebCargo", file=out)
    print("   Emails sent       none", file=out)
    print("   Quotation sent    none", file=out)
    print(RULE, file=out)
    return EXIT_OK
