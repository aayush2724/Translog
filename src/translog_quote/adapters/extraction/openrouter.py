"""The OpenRouter extraction adapter — the one place a language model is called.

Implements `ports.ExtractionPort` using the Phase 4 contract unchanged: the
prompt comes from `domain.extraction.prompt`, and the response is validated
against `domain.extraction.ExtractionResult`. Nothing provider-shaped crosses
back out — callers receive an `ExtractionResult` or an exception from the
project's own taxonomy.

**Structured output.** OpenRouter's live model metadata reports
``qwen/qwen3.7-flash`` as supporting ``response_format`` but **not**
``structured_outputs``. So this adapter asks for JSON mode
(``{"type": "json_object"}``) rather than sending a ``json_schema``, and does
the schema enforcement itself against `ExtractionResult`. That is strictly
safer than trusting an unsupported parameter to be honoured: the model is asked
for JSON, and the contract is enforced here regardless of what comes back.
"""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING, Any

from translog_quote.domain.extraction import (
    ExtractionResult,
    FieldStatus,
    build_extraction_messages,
)
from translog_quote.domain.extraction.prompt import with_reply_context
from translog_quote.domain.extraction.ship_date import (
    DATE_ORDER_MARKER,
    WEEKDAY_MARKER,
    ambiguous_day_month,
    either_way_date,
    weekday_mismatch,
)
from translog_quote.domain.shipment import units
from translog_quote.errors import ContractViolation
from translog_quote.observability import get_logger

if TYPE_CHECKING:
    from translog_quote.adapters.extraction.transport import ChatTransport
    from translog_quote.domain.decision import ClientIntent

_log = get_logger("adapters.extraction.openrouter")

_JSON_FENCE = "```"


class OpenRouterExtractionAdapter:
    """Extracts shipment fields from one email via a chat-completions model.

    ``temperature=0`` and a fixed ``seed`` because extraction is not a creative
    task: the same email should yield the same fields on Tuesday as it did on
    Monday, and a demo that drifts is not a demonstration. Neither makes the
    model deterministic in the strict sense, but both remove the variation we
    are able to remove.
    """

    def __init__(
        self,
        *,
        transport: ChatTransport,
        model: str,
        temperature: float = 0.0,
        seed: int | None = 1,
        max_tokens: int = 2048,
    ) -> None:
        if not model:
            raise ContractViolation(
                "No extraction model configured. Set TRANSLOG_OPENROUTER__MODEL."
            )
        self._transport = transport
        self._model = model
        self._temperature = temperature
        self._seed = seed
        self._max_tokens = max_tokens

    # ---------------------------------------------------------------- port --

    def close(self) -> None:
        """Release the transport's pooled connection, if it has one.

        Duck-typed rather than declared on `ChatTransport`: that protocol describes
        one operation, and widening it to a lifecycle would oblige every test
        double and fixture in the suite to grow a method none of them need.
        """
        closer = getattr(self._transport, "close", None)
        if callable(closer):
            closer()

    def extract_shipment(self, text: str, *, asked_about: tuple[str, ...] = ()) -> ExtractionResult:
        payload = self._build_payload(with_reply_context(asked_about, text))
        body = self._transport.post_chat_completion(payload)
        content = self._content_of(body)
        return self._parse(content)

    def read_client_intent(self, text: str) -> ClientIntent:
        """Not implemented — deliberately, and not a stub that guesses.

        Phase 4 defined an extraction contract for shipment fields only. There is
        no prompt, no schema and no agreed semantics for reading accept/reject
        intent, and inventing them here would put an undefined contract in front
        of a commercial decision. Client ACCEPT/REJECT is Phase 12.
        """
        raise NotImplementedError(
            "read_client_intent is not implemented. The client accept/reject "
            "contract is defined in Phase 12; see docs/extraction-contract.md §16."
        )

    # ------------------------------------------------------------- request --

    def _build_payload(self, email_body: str) -> dict[str, Any]:
        """Assemble the request. The email never leaves its own user turn.

        `build_extraction_messages` (Phase 4) returns the instructions as system
        turns and the fenced email as a single user turn. This method only
        renames the tuple fields into the provider's wire shape — it does not
        merge, reorder or reformat them, because the separation between
        instruction and untrusted content is the injection defence.
        """
        messages = [
            {"role": role, "content": content}
            for role, content in build_extraction_messages(email_body)
        ]

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            # JSON mode. Not `json_schema`: the model does not advertise
            # `structured_outputs`, and asking for an unsupported enforcement
            # mode would be worse than enforcing it ourselves, which we do.
            #
            # The instructions must name JSON for this to be accepted at all --
            # the provider rejects a JSON-mode request whose messages never say
            # the word. `domain.extraction.prompt` says it, and a test keeps it
            # said.
            "response_format": {"type": "json_object"},
            # Qwen 3.7 Flash is a reasoning model, and reasoning tokens are
            # charged against `max_tokens`. Left on, it spends the entire budget
            # thinking and returns `finish_reason=length` with empty content --
            # observed at exactly 2048 reasoning tokens and 0 characters of
            # answer. Extraction is mechanical transcription, not a problem to
            # reason about, so reasoning is off: the same request then returns a
            # complete JSON object in ~440 completion tokens.
            #
            # `{"effort": "minimal"}` does not help -- it still spent the full
            # 2048. Only disabling outright works.
            "reasoning": {"enabled": False},
        }
        if self._seed is not None:
            payload["seed"] = self._seed

        return payload

    # ------------------------------------------------------------ response --

    def _content_of(self, body: dict[str, Any]) -> str:
        """Pull the assistant message text out of a chat-completions envelope."""
        error = body.get("error")
        if error is not None:
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ContractViolation(f"OpenRouter reported an error: {message}")

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ContractViolation("Model response contained no choices")

        first = choices[0]
        if not isinstance(first, dict):
            raise ContractViolation("Model response choice was not an object")

        finish_reason = first.get("finish_reason")
        if finish_reason == "length":
            # Truncated JSON parses as malformed or, worse, as a smaller valid
            # object. Either way the extraction is incomplete and must not be
            # treated as an email that simply said less.
            raise ContractViolation(
                "Model response was truncated (finish_reason=length); "
                "extraction is incomplete and cannot be trusted"
            )

        message = first.get("message")
        if not isinstance(message, dict):
            raise ContractViolation("Model response choice carried no message")

        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ContractViolation("Model returned empty content")

        return content

    def _parse(self, content: str) -> ExtractionResult:
        """Strict validation against the Phase 4 contract.

        A response that does not satisfy the schema is a failure of the model,
        not an email that stated nothing. It raises rather than returning an
        empty result, because an empty result is indistinguishable from a
        genuinely uninformative email and would send a pointless clarification
        to a client who had already told us everything.
        """
        raw = _strip_code_fence(content)

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractViolation(
                f"Model did not return valid JSON: {exc.msg} at position {exc.pos}"
            ) from exc

        if not isinstance(payload, dict):
            raise ContractViolation(
                f"Model returned a JSON {type(payload).__name__}, expected an object"
            )

        _move_cargo_type_out_of_commodity(payload)
        _normalise_units(payload)
        _flag_ambiguous_day_month(payload)
        _flag_weekday_mismatch(payload)
        _mark_impossible_values_invalid(payload)

        try:
            result = ExtractionResult.model_validate(payload)
        except Exception as exc:
            raise ContractViolation(
                f"Model output did not satisfy the extraction contract: {exc}"
            ) from exc

        _log.debug(
            "extraction complete: %d field(s) stated",
            len(result.fields_by_status(FieldStatus.STATED)),
        )
        return result


#: Internal notes recorded on a field rewritten to INVALID. Client-facing wording
#: lives in the clarification layer (``invalid_question``); this is evidence.
_IMPOSSIBLE_NOTES = {
    "weight_kg": "stated gross weight is not greater than 0",
    "pcs": "stated number of pieces is not greater than 0",
    "dimensions_in": "a stated dimension is not greater than 0",
}


#: Spellings the model may use for "the email gave no unit".
_NO_UNIT = {"", "none", "null", "unknown", "unspecified", "not stated", "n/a"}

_SIDES = ("length", "width", "height")


def _number_text(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _normalise_units(payload: dict[str, Any]) -> None:
    """Convert a stated weight or set of dimensions to kilograms / inches, in
    place, before the contract is validated.

    The model reports measurements exactly as written plus the unit it saw, in a
    ``unit`` key on the field (for dimensions it may also sit inside ``value``).
    This step does the arithmetic in code (`domain.shipment.units`) — never the
    model — and removes the ``unit`` key so the contract sees only canonical
    values. A value with no unit, or a unit that cannot be converted, becomes an
    INVALID field carrying a unit note: the client knows the unit, so it is
    asked for in a clarification instead of being guessed or handed to a person.

    A field with no ``unit`` key at all is left untouched: that is the original
    contract (kilograms / inches only), so older-format output still parses.
    Anything malformed is also left untouched and still fails ``model_validate``.

    On any status other than ``stated`` a ``unit`` key carries no information
    and is simply dropped: the live model writes ``"unit": null`` on a
    ``not_stated`` weight, and rejecting the whole extraction over it would hand
    a perfectly good reply to a person.
    """
    for field in ("weight_kg", "dimensions_in"):
        cell = payload.get(field)
        if isinstance(cell, dict) and cell.get("status") != "stated":
            cell.pop("unit", None)
            if isinstance(cell.get("value"), dict):
                cell["value"].pop("unit", None)

    weight = payload.get("weight_kg")
    if isinstance(weight, dict) and weight.get("status") == "stated" and "unit" in weight:
        raw = weight.pop("unit")
        value = weight.get("value")
        if isinstance(value, int | float) and not isinstance(value, bool):
            seen = _number_text(value)
            canon = units.weight_unit(raw) if isinstance(raw, str) else None
            written = units.dot_thousands_in(weight.get("evidence"), float(value))
            if written is not None:
                # "1.200 kg" read as 1.2: maybe 1,200. Asked, never guessed.
                payload["weight_kg"] = {
                    "status": "invalid",
                    "evidence": weight.get("evidence"),
                    "note": units.unit_note("Weight", units.SEPARATOR_MARKER, written),
                }
            elif canon is not None and units.is_chargeable_only(weight.get("evidence")):
                # The airline's billing figure, not the actual weight. Asked.
                payload["weight_kg"] = {
                    "status": "invalid",
                    "evidence": weight.get("evidence"),
                    "note": units.unit_note("Weight", units.CHARGEABLE_MARKER, f"{seen} {raw}"),
                }
            elif canon is not None:
                if units.kilo_suffix_in(weight.get("evidence"), float(value)):
                    value = value * 1000  # "1.5k kg" is 1,500 kg
                    _log.info("extraction: weight %sk read as %s", seen, _number_text(value))
                kg = units.to_kg(value, canon)
                if canon != "kg":
                    _log.info("extraction: weight converted from %s %s to kg", seen, raw)
                payload["weight_kg"] = {**weight, "value": kg}
                if units.is_per_piece(weight.get("evidence")):
                    _total_per_piece_weight(payload, weight, kg, f"{seen} {raw}")
            else:
                missing = raw is None or (isinstance(raw, str) and raw.strip().lower() in _NO_UNIT)
                marker = units.UNIT_MISSING_MARKER if missing else units.UNIT_UNSUPPORTED_MARKER
                shown = seen if missing else f"{seen} {raw}"
                payload["weight_kg"] = {
                    "status": "invalid",
                    "evidence": weight.get("evidence"),
                    "note": units.unit_note("Weight", marker, shown),
                }

    dims = payload.get("dimensions_in")
    if not (isinstance(dims, dict) and dims.get("status") == "stated"):
        return
    value = dims.get("value")
    if not isinstance(value, dict):
        return
    if "unit" in dims:
        raw = dims.pop("unit")
    elif "unit" in value:
        raw = value.pop("unit")
    else:
        return
    raw_sides = [value.get(side) for side in _SIDES]
    if (isinstance(raw, str) and units.is_feet_inches(raw)) or any(
        isinstance(s, str) for s in raw_sides
    ):
        _convert_feet_inches(payload, dims, raw_sides, raw)
        return
    if not all(isinstance(s, int | float) and not isinstance(s, bool) for s in raw_sides):
        return  # malformed — left for model_validate to reject
    sides = [float(s) for s in raw_sides]  # type: ignore[arg-type]  # checked just above
    seen = " x ".join(_number_text(s) for s in raw_sides)
    canon = units.length_unit(raw) if isinstance(raw, str) else None
    if canon is not None:
        converted = {side: units.to_inches(s, canon) for side, s in zip(_SIDES, sides, strict=True)}
        payload["dimensions_in"] = {**dims, "value": converted}
        if canon != "in":
            _log.info("extraction: dimensions converted from %s %s to inches", seen, raw)
        return
    missing = raw is None or (isinstance(raw, str) and raw.strip().lower() in _NO_UNIT)
    marker = units.UNIT_MISSING_MARKER if missing else units.UNIT_UNSUPPORTED_MARKER
    shown = seen if missing else f"{seen} {raw}"
    payload["dimensions_in"] = {
        "status": "invalid",
        "evidence": dims.get("evidence"),
        "note": units.unit_note("Dimensions", marker, shown),
    }


def _total_per_piece_weight(
    payload: dict[str, Any], weight: dict[str, Any], kg: float, shown: str
) -> None:
    """A weight given per piece ("50 kg each"): multiplied by the stated piece
    count, or — with no usable count — asked for as the shipment total."""
    pcs = payload.get("pcs")
    count = pcs.get("value") if isinstance(pcs, dict) and pcs.get("status") == "stated" else None
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        payload["weight_kg"] = {**weight, "value": units.times_pieces(kg, count)}
        _log.info("extraction: per-piece weight %s multiplied by %d pieces", shown, count)
        return
    payload["weight_kg"] = {
        "status": "invalid",
        "evidence": weight.get("evidence"),
        "note": units.unit_note("Weight", units.PER_PIECE_MARKER, shown),
    }


def _convert_feet_inches(
    payload: dict[str, Any], dims: dict[str, Any], raw_sides: list[object], raw: object
) -> None:
    """Dimensions with a side written as text: feet and inches ("2'6\" x 1'8\"
    x 1'4\""), or a fraction under an ordinary unit ("1 1/2 ft"). Each side is
    parsed in code. A bare number under the feet-and-inches unit is whole feet.
    Any side that does not parse makes the field INVALID with the client's
    wording, so it is asked about — never guessed."""
    seen = " x ".join(_number_text(s) for s in raw_sides)
    feet_inches = isinstance(raw, str) and units.is_feet_inches(raw)
    canon = None if feet_inches else (units.length_unit(raw) if isinstance(raw, str) else None)
    inches: list[float] = []
    for side in raw_sides:
        parsed: float | None = None
        number = isinstance(side, int | float) and not isinstance(side, bool) and side > 0
        if canon is not None:
            if isinstance(side, str):
                parsed = units.parse_side(side, canon)
            elif number:
                parsed = units.to_inches(float(side), canon)  # type: ignore[arg-type]
        elif isinstance(side, str):
            parsed = units.parse_feet_inches(side)
        elif number and feet_inches:
            parsed = float(side) * 12  # type: ignore[arg-type]
        if parsed is None:
            payload["dimensions_in"] = {
                "status": "invalid",
                "evidence": dims.get("evidence"),
                "note": units.unit_note("Dimensions", units.UNIT_UNSUPPORTED_MARKER, seen),
            }
            return
        inches.append(parsed)
    payload["dimensions_in"] = {
        **dims,
        "value": dict(zip(_SIDES, inches, strict=True)),
    }
    _log.info("extraction: dimensions converted from %s (feet and inches) to inches", seen)


def _flag_ambiguous_day_month(payload: dict[str, Any]) -> None:
    """Turn a stated numeric date that could be either day/month order
    ("05/11/2026") into an AMBIGUOUS field, in place.

    The model reads such dates day-first, which is right for most clients and
    silently wrong for one who writes month-first. An ambiguous shipment date is
    re-asked (never handed to a person), and the clarification quotes both
    readings so the client only has to pick one.
    """
    cell = payload.get("ship_date")
    if isinstance(cell, dict) and cell.get("status") == "ambiguous":
        # The model saw the problem itself; give it the note that asks the
        # client to pick between the two readings.
        written = either_way_date(cell.get("evidence")) or either_way_date(cell.get("note"))
        if written is not None:
            cell["note"] = f"Ship date {DATE_ORDER_MARKER}: {written}"
        return
    if not (isinstance(cell, dict) and cell.get("status") == "stated"):
        return
    value = cell.get("value")
    if not isinstance(value, str):
        return
    try:
        stated = date.fromisoformat(value)
    except ValueError:
        return
    found = ambiguous_day_month(cell.get("evidence"), stated)
    if found is None:
        return
    written, _day_first, _month_first = found
    payload["ship_date"] = {
        "status": "ambiguous",
        "evidence": cell.get("evidence"),
        "note": f"Ship date {DATE_ORDER_MARKER}: {written}",
    }


def _flag_weekday_mismatch(payload: dict[str, Any]) -> None:
    """Turn a stated date whose written weekday is wrong ("Monday, 29 October
    2026", a Thursday) into an AMBIGUOUS field, in place, so the client is asked
    which of the two they meant."""
    cell = payload.get("ship_date")
    if not (isinstance(cell, dict) and cell.get("status") == "stated"):
        return
    value = cell.get("value")
    if not isinstance(value, str):
        return
    try:
        stated = date.fromisoformat(value)
    except ValueError:
        return
    named = weekday_mismatch(cell.get("evidence"), stated)
    if named is None:
        return
    payload["ship_date"] = {
        "status": "ambiguous",
        "evidence": cell.get("evidence"),
        "note": f"Ship date {WEEKDAY_MARKER}: {named}, {stated.isoformat()}",
    }


#: Phrases that describe the cargo's *type*, never what it is. The model reads
#: a short reply such as "general cargo, chemical: no" as naming the commodity;
#: recorded there it conflicts with the real commodity and the client is asked
#: a nonsensical question ("Printed books or general cargo?").
_CARGO_TYPE_PHRASES = frozenset(
    {
        "general cargo",
        "general",
        "gc",
        "gen cargo",
        "non haz",
        "nonhaz",
        "non hazardous",
        "non hazardous cargo",
        "non haz cargo",
        "ndg",
        "non dg",
        "non dangerous goods",
        "dg",
        "dangerous goods",
        "haz",
        "hazardous",
        "hazardous cargo",
    }
)


def _move_cargo_type_out_of_commodity(payload: dict[str, Any]) -> None:
    """A stated commodity that is only a cargo-type phrase is moved to
    ``cargo_type`` (when that is not already stated) and the commodity left
    not stated, in place."""
    commodity = payload.get("commodity")
    if not (isinstance(commodity, dict) and commodity.get("status") == "stated"):
        return
    value = commodity.get("value")
    if not isinstance(value, str):
        return
    key = " ".join(value.lower().replace("-", " ").replace(".", " ").split())
    if key not in _CARGO_TYPE_PHRASES:
        return
    cargo = payload.get("cargo_type")
    if not (isinstance(cargo, dict) and cargo.get("status") == "stated"):
        payload["cargo_type"] = dict(commodity)
    payload["commodity"] = {"status": "not_stated", "value": None}


def _is_nonpositive_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value <= 0


def _mark_impossible_values_invalid(payload: dict[str, Any]) -> None:
    """Rewrite a *stated* out-of-range weight/pieces/dimension to an ``INVALID``
    field, in place, before the contract is validated.

    This is the whole "invalid but understandable" boundary: a value the client
    clearly stated but that is out of range (``-5`` pieces, ``0`` kg, a ``0``
    dimension) is understood and correctable, so it becomes an ``INVALID`` field
    that drives a clarification asking for a valid value. It is *not* a malformed
    extraction: everything else — bad JSON, a wrong shape, a non-numeric value —
    is left untouched and still fails ``ContractViolation`` at ``model_validate``,
    so an extraction that cannot be trusted to name a field still hands over to a
    person, exactly as before.
    """
    for field in ("weight_kg", "pcs"):
        cell = payload.get(field)
        if (
            isinstance(cell, dict)
            and cell.get("status") == "stated"
            and _is_nonpositive_number(cell.get("value"))
        ):
            payload[field] = {"status": "invalid", "note": _IMPOSSIBLE_NOTES[field]}

    dims = payload.get("dimensions_in")
    if isinstance(dims, dict) and dims.get("status") == "stated":
        value = dims.get("value")
        if isinstance(value, dict) and any(
            _is_nonpositive_number(value.get(side)) for side in ("length", "width", "height")
        ):
            payload["dimensions_in"] = {
                "status": "invalid",
                "note": _IMPOSSIBLE_NOTES["dimensions_in"],
            }


def _strip_code_fence(content: str) -> str:
    """Unwrap ```json ... ``` if the model wrapped its output in one.

    JSON mode usually prevents this, but not every model honours it perfectly,
    and a fenced-but-otherwise-perfect response is a formatting quirk rather
    than a contract breach. Anything still malformed after unwrapping fails
    loudly in `_parse`.
    """
    text = content.strip()
    if not text.startswith(_JSON_FENCE):
        return text

    without_open = text[len(_JSON_FENCE) :]
    if without_open.lower().startswith("json"):
        without_open = without_open[4:]

    closing = without_open.rfind(_JSON_FENCE)
    if closing != -1:
        without_open = without_open[:closing]

    return without_open.strip()
