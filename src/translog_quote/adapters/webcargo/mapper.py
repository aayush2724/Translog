"""Turning a WebCargo response into the internal `Rate` model.

Adapter-owned and separately testable: a mapper translates one wire shape and
does nothing else. It never drops a row and never reorders — a row it cannot map
becomes a `Rate` with null fields and is excluded later, by a filter, with a
reason. A mapper that silently discarded rows would hide exactly the integration
gaps this stage exists to surface.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from translog_quote.domain.rates import Rate, RateRestrictions, TransitTime
from translog_quote.errors import ContractViolation, UnresolvedFieldMapping


class RealRateMapper:
    """Maps only the fields the specification actually documents.

    Transit time is the exception, and it is an executable blocker rather than a
    comment: anyone wiring the real adapter hits a named error pointing at the
    unresolved question, instead of shipping a plausible wrong field.
    """

    def map_transit(self, row: dict[str, Any]) -> TransitTime:
        raise UnresolvedFieldMapping(
            "WebCargo transit-time source is unverified. The documented response "
            "carries no transit time, flight date or routing, and transit is the "
            "primary ranking key (AMB-1). Confirm the source before mapping it."
        )

    def map_row(self, row: dict[str, Any]) -> Rate:
        """One documented row. Raises on a shape that is not the documented one.

        External data is validated before it enters the domain: a row that is
        not an object, or whose price is not a number, is a contract violation
        rather than a rate with surprising contents.
        """
        if not isinstance(row, dict):
            raise ContractViolation(f"rate row was {type(row).__name__}, expected an object")

        carrier = row.get("airline")
        if not isinstance(carrier, str) or not carrier.strip():
            raise ContractViolation("rate row has no usable 'airline'")

        product = row.get("product")
        if not isinstance(product, str):
            raise ContractViolation("rate row has no usable 'product'")

        return Rate(
            carrier_code=carrier.strip()[:3].upper(),
            carrier_name=carrier.strip(),
            product=product.strip(),
            total_amount=_as_decimal(row.get("total")),
            currency=row.get("currency") if isinstance(row.get("currency"), str) else None,
            # Left unmapped on purpose. Filtering excludes it as unrankable and
            # says so, which is the honest report of an unresolved mapping.
            transit=None,
            restrictions=RateRestrictions(
                accepts_liquids=row["accepts_liquids"]
                if isinstance(row.get("accepts_liquids"), bool)
                else None
            ),
            source_ref="webcargo",
        )


def _as_decimal(value: object) -> Decimal | None:
    """A price, or nothing. Never a partially-parsed number."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ContractViolation("rate 'total' was a boolean")
    if isinstance(value, int | float | str):
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise ContractViolation(f"rate 'total' is not a number: {value!r}") from exc
    raise ContractViolation(f"rate 'total' was {type(value).__name__}")
