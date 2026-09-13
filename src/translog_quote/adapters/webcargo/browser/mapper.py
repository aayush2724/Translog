"""Turning captured WebCargo rows into canonical `Rate` values.

The mapper contract the demo established, kept: never drops a row, never
reorders. A row whose price or duration cannot be read becomes a `Rate` with
the corresponding field ``None``, and the *filter* excludes it later with a
reason — an unreadable field surfaces as a visible exclusion, not a silent
disappearance and not a guessed value.

Transit comes from exactly one place: the row's provider-stated Duration.
`parse_duration` accepts a duration string and nothing else — itinerary,
legs, departure and arrival are not arguments and cannot become inputs
without changing signatures. Official data only.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from translog_quote.domain.rates import Rate, RateRestrictions, TransitTime, TransitUnit

if TYPE_CHECKING:
    from translog_quote.adapters.webcargo.browser.records import WebCargoRateRecord

ADAPTER_ID = "webcargo-browser"

#: WebCargo displays rate-associated transit as e.g. "34h 00m", "18h 45m",
#: "75h 20m". Anything else — including empty — is "the provider did not
#: state a usable duration" and maps to None, never to a guess.
_DURATION = re.compile(r"^\s*(\d+)\s*h(?:\s*([0-5]?\d)\s*m)?\s*$")

#: One money token as WebCargo prints it: "40,000 Rs", "83,825 Rs",
#: "1,23,456 Rs" (Indian grouping), "400.00 Rs". Group 1 the figure,
#: group 2 the currency token exactly as displayed.
_MONEY = re.compile(r"([0-9][0-9.,]*)\s*([A-Za-z]{1,5})\s*$")

#: A leading airline designator inside the service name: "QR General",
#: "TK URGENT". Two characters, letters/digits, as IATA prints them.
_SERVICE_CODE = re.compile(r"^([A-Z][A-Z0-9]|[0-9][A-Z])\s+\S")


def parse_duration(duration: str) -> TransitTime | None:
    """The provider's stated duration, in minutes — or None, never a guess.

    A stated zero is refused too: "0h 0m" is not a shipment duration, and a
    rate carrying it must surface as unrankable rather than as the
    instantly-fastest option in the ranking.
    """
    match = _DURATION.match(duration)
    if match is None:
        return None
    minutes = int(match.group(1)) * 60 + int(match.group(2) or 0)
    if minutes <= 0:
        return None
    return TransitTime(value=minutes, unit=TransitUnit.MINUTES)


def parse_price(price_block: str) -> tuple[Decimal | None, str | None]:
    """The shipment total and its displayed currency token.

    The observed block is "400.00 Rs/kg/ 40,000 Rs" — per-kg figure, then
    the total. The total is the last "/"-separated segment; a block with no
    parseable total yields ``(None, None)``, which BR-4 then excludes as
    not-an-offer. Thousands separators (including Indian grouping) are
    removed; nothing else is repaired.
    """
    tail = price_block.split("/")[-1].strip().removesuffix("(+)").strip()
    match = _MONEY.search(tail)
    if match is None:
        return None, None
    figure = match.group(1).replace(",", "")
    try:
        return Decimal(figure), match.group(2)
    except InvalidOperation:
        return None, None


def carrier_code_of(record: WebCargoRateRecord) -> str:
    """The airline's own designator, read from what WebCargo displayed.

    Preference order, all provider data: the code opening the service name
    ("QR General"), else the code opening a leg's flight number ("QR573"),
    else the company name verbatim. The last is not an IATA code, and
    deliberately so — inventing one would be fabrication, and the code's
    only ranking role is the deterministic final tie-break.
    """
    service_match = _SERVICE_CODE.match(record.service.strip())
    if service_match:
        return service_match.group(1)
    for leg in record.legs:
        flight = leg.flight_number.strip()
        if re.match(r"^[A-Z0-9]{2}\d+$", flight):
            return flight[:2]
    return record.company.strip()


def map_record(record: WebCargoRateRecord) -> Rate:
    """One captured row, as a canonical Rate. Nothing invented anywhere.

    No restriction fields are populated: this surface declares neither a
    liquids policy nor door-delivery capability, and an undeclared
    capability must stay undeclared (the service filter treats "did not
    say" as "not offered", which is the safe polarity).
    """
    total, currency = parse_price(record.price)
    return Rate(
        carrier_code=carrier_code_of(record),
        carrier_name=record.company.strip(),
        product=record.service.strip(),
        total_amount=total,
        currency=currency,
        transit=parse_duration(record.duration),
        restrictions=RateRestrictions(),
        source_ref=f"{ADAPTER_ID}:{record.date_tab}:{record.service.strip()}"
        f":{record.departure.strip()}",
        departure_date_label=record.date_tab.strip(),
    )


def map_records(records: tuple[WebCargoRateRecord, ...]) -> tuple[Rate, ...]:
    """Every captured row, order preserved, membership preserved."""
    return tuple(map_record(record) for record in records)
