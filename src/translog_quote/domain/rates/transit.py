"""Deriving a shipment duration from a provider's own departure and arrival.

Used only when a provider states two instants rather than a duration. Nothing
here reads a route: `Via`, connection counts, airline names and product codes
are not arguments to this function and cannot become one without changing its
signature, so a duration can never be inferred from routing (official data
only).

The rule this module exists to make unrepresentable: a duration is never
computed from displayed local clock times. WebCargo shows each stamp in its own
airport's zone, so subtracting the readings is wrong by exactly the offset
between them — a Bangalore departure and a Manila arrival are 2h30m apart
before a single minute of flying is counted. Both arguments must therefore
carry a timezone, and a naive one is refused rather than assumed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.domain.rates.model import TransitTime, TransitUnit

if TYPE_CHECKING:
    from datetime import datetime


class UndeterminableTransit(Exception):
    """The provider's data does not support a duration, so none is produced.

    Raised instead of returning a best guess. A caller catches this and leaves
    `Rate.transit` as ``None``, which the eligibility filter then excludes as
    unrankable *with a reason* — the honest report of missing provider data,
    never a rate that looks rankable because a number was invented for it.
    """


def elapsed_transit(departure: datetime, arrival: datetime) -> TransitTime:
    """Elapsed shipment duration between two timezone-aware instants.

    Returns minutes, not hours: a real departure-to-arrival span is rarely a
    whole number of hours, and rounding one so that it can be ranked would put
    an approximated figure behind a quotation.

    Raises `UndeterminableTransit` when either stamp is naive, or when arrival
    does not follow departure. Both are refusals rather than repairs — there is
    no correct way to guess which zone a bare timestamp was displayed in.
    """
    for name, moment in (("departure", departure), ("arrival", arrival)):
        if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
            raise UndeterminableTransit(
                f"{name} timestamp carries no timezone; an elapsed duration cannot be "
                "derived from displayed local clock times"
            )

    minutes = round((arrival - departure).total_seconds() / 60)
    if minutes <= 0:
        raise UndeterminableTransit(
            "arrival does not follow departure; no shipment duration can be derived"
        )

    return TransitTime(value=minutes, unit=TransitUnit.MINUTES)
