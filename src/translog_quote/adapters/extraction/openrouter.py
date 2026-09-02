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
from typing import TYPE_CHECKING, Any

from translog_quote.domain.extraction import (
    ExtractionResult,
    FieldStatus,
    build_extraction_messages,
)
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

    def extract_shipment(self, text: str) -> ExtractionResult:
        payload = self._build_payload(text)
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
