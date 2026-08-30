"""Hard eligibility filtering. Stage three of the rate pipeline.

Filtering changes membership and nothing else — it never scores, never reorders,
and never rewrites a rate. Every exclusion carries a reason, because the
quotation maker has to see *why* a carrier is absent; silence there is
indistinguishable from a bug.

Only the rules the project actually documents are implemented. Nothing about
price, aircraft, service level, minimum or maximum weight, dangerous goods or
carrier preference appears here, because none of it is documented.
"""

from __future__ import annotations

from translog_quote.domain.rates.model import (
    ExcludedRate,
    ExclusionReason,
    FilterOutcome,
    Rate,
)


def drop_incomplete_rate(rate: Rate) -> ExcludedRate | None:
    """BR-4 — a rate with no price is not an offer.

    The specification's own worked example drops a carrier on exactly this rule.
    """
    if rate.total_amount is None or rate.currency is None:
        return ExcludedRate(
            rate=rate,
            reason=ExclusionReason.INCOMPLETE_RATE,
            detail="no price returned for this carrier",
        )
    return None


def drop_unrankable_rate(rate: Rate) -> ExcludedRate | None:
    """A rate with no transit time cannot be ranked under BR-1.

    Not merely unattractive — unrankable, because transit is the primary key.
    Excluding it silently would let a real-adapter mapping gap masquerade as a
    thin market, so the exclusion is recorded per rate and the caller can tell
    the two apart.
    """
    if rate.transit is None:
        return ExcludedRate(
            rate=rate,
            reason=ExclusionReason.UNRANKABLE_NO_TRANSIT,
            detail="no transit time available; cannot be ranked on speed",
        )
    return None


def drop_restricted_carrier(rate: Rate, *, cargo_is_liquid: bool | None) -> ExcludedRate | None:
    """BR-5 — a carrier that cannot carry this cargo.

    The only restriction the project documents is that one carrier does not
    accept liquid products, so that is the only one implemented.

    ``cargo_is_liquid`` is a required argument with no default and is never
    derived here. The canonical record cannot express physical form, and
    inferring "liquid" from a commodity name is exactly the kind of guess that
    would put a wrong carrier on a real quotation (AMB-3). When it is ``None``
    the fact is unknown, and an unknown fact excludes nothing.
    """
    if cargo_is_liquid is not True:
        return None
    if rate.restrictions.accepts_liquids is False:
        return ExcludedRate(
            rate=rate,
            reason=ExclusionReason.CARRIER_RESTRICTED,
            detail=f"{rate.carrier_name} does not accept liquid products",
        )
    return None


def drop_service_mismatch(rate: Rate, *, requires_door_delivery: bool) -> ExcludedRate | None:
    """A rate that cannot perform the delivery scope the client stated.

    Only ``True`` passes. ``None`` — the provider did not declare the capability
    — excludes just as ``False`` does, which is the opposite polarity from the
    liquids rule and deliberately so: quoting door delivery on a rate that never
    promised it is a commitment made on the client's behalf, and the shipment
    was validated against the client's own words (VR-10). An unknown liquid
    restriction loses us an option; an unknown delivery capability must never
    gain us one.
    """
    if not requires_door_delivery:
        return None
    if rate.restrictions.serves_door_delivery is not True:
        return ExcludedRate(
            rate=rate,
            reason=ExclusionReason.SERVICE_NOT_AVAILABLE,
            detail=(
                f"{rate.carrier_name} does not offer door delivery on this rate"
                if rate.restrictions.serves_door_delivery is False
                else f"{rate.carrier_name} does not state door delivery for this rate; "
                "an undeclared capability is not offered"
            ),
        )
    return None


def filter_rates(
    rates: tuple[Rate, ...],
    *,
    cargo_is_liquid: bool | None = None,
    requires_door_delivery: bool = False,
) -> FilterOutcome:
    """Run every filter over every rate, preserving order.

    A rate is excluded by the first rule that rejects it, so an exclusion reason
    is the *primary* reason rather than an arbitrary one from a set.
    """
    eligible: list[Rate] = []
    excluded: list[ExcludedRate] = []

    for rate in rates:
        rejection = (
            drop_incomplete_rate(rate)
            or drop_unrankable_rate(rate)
            or drop_restricted_carrier(rate, cargo_is_liquid=cargo_is_liquid)
            or drop_service_mismatch(rate, requires_door_delivery=requires_door_delivery)
        )
        if rejection is None:
            eligible.append(rate)
        else:
            excluded.append(rejection)

    return FilterOutcome(eligible=tuple(eligible), excluded=tuple(excluded))
