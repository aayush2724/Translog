"""HTTP transport for OpenRouter chat completions.

Everything in this module is an HTTP concern: the endpoint, the auth header,
timeouts, status codes and retries. It knows nothing about shipments, prompts or
extraction, and it returns a parsed JSON body rather than any provider object —
so the adapter above it never handles an ``httpx`` type, and neither does the
domain below.

Provider failures are translated into the project's own taxonomy here, at the
edge, so that no ``httpx`` exception escapes ``adapters/``.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

from translog_quote.errors import PermanentFailure, TransientFailure
from translog_quote.observability import get_logger

_log = get_logger("adapters.extraction.transport")

_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class ChatTransport(Protocol):
    """Sends one chat-completion request and returns the decoded JSON body.

    A seam, so the adapter's contract handling can be tested without a network
    or an API key. Implementations raise ``TransientFailure`` or
    ``PermanentFailure`` and nothing else.
    """

    def post_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class HttpxChatTransport:
    """The real transport. One POST to ``{base_url}/chat/completions``.

    The API key is held as plain text here because it has to be — it goes into
    an Authorization header. It is never logged, never placed in an exception
    message, and never returned. Every log line and error below names the status
    code and the model, and nothing else from the request.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: int,
        max_retries: int,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise PermanentFailure(
                "No OpenRouter API key configured. Set TRANSLOG_OPENROUTER__API_KEY."
            )
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            # OpenRouter uses these for attribution. Neither carries client data.
            "HTTP-Referer": "https://github.com/translog/quote-demo",
            "X-Title": "Translog Cargo Quotation Demo",
        }

    def post_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        attempts = self._max_retries + 1
        last_transient: TransientFailure | None = None

        for attempt in range(1, attempts + 1):
            try:
                return self._attempt(payload)
            except TransientFailure as exc:
                last_transient = exc
                _log.warning(
                    "extraction request failed (attempt %d/%d): %s", attempt, attempts, exc
                )

        assert last_transient is not None  # only reachable via the except branch
        raise last_transient

    def _attempt(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if self._client is not None:
                response = self._client.post(
                    self._url, json=payload, headers=self._headers(), timeout=self._timeout
                )
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(self._url, json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise TransientFailure(f"OpenRouter request timed out after {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            # Deliberately not interpolating the exception's own text: httpx
            # includes the request URL, and the URL is the one place a key could
            # appear if it were ever moved to a query parameter.
            raise TransientFailure(
                f"OpenRouter request failed at the transport layer: {type(exc).__name__}"
            ) from exc

        return self._decode(response)

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        status = response.status_code

        if status in _RETRYABLE_STATUS:
            raise TransientFailure(f"OpenRouter returned {status}")

        if status == 401 or status == 403:
            raise PermanentFailure(
                f"OpenRouter rejected the credentials ({status}). "
                "Check TRANSLOG_OPENROUTER__API_KEY."
            )

        if status >= 400:
            raise PermanentFailure(f"OpenRouter returned {status}: {_safe_detail(response)}")

        try:
            body = response.json()
        except ValueError as exc:
            raise PermanentFailure("OpenRouter returned a non-JSON body") from exc

        if not isinstance(body, dict):
            raise PermanentFailure(
                f"OpenRouter returned {type(body).__name__}, expected a JSON object"
            )

        return body


_DETAIL_LIMIT = 400


def _safe_detail(response: httpx.Response) -> str:
    """A short, bounded description of an error body.

    Reads only the response. The request, its headers and the API key are never
    involved, and an OpenRouter error body carries none of them.

    OpenRouter wraps an upstream provider's failure and reports its own message
    as a flat "Provider returned error", putting the provider's actual complaint
    in ``error.metadata.raw`` as an SSE-framed JSON string. Surfacing only the
    outer message throws away the one sentence that says what is wrong — so this
    unwraps one level when the metadata is there.
    """
    try:
        payload = response.json()
    except ValueError:
        return response.text[:_DETAIL_LIMIT]

    if not isinstance(payload, dict):
        return str(payload)[:_DETAIL_LIMIT]

    error = payload.get("error")
    if not isinstance(error, dict):
        return str(error)[:_DETAIL_LIMIT] if error is not None else str(payload)[:_DETAIL_LIMIT]

    parts: list[str] = [str(error.get("message", "")).strip() or "unspecified error"]

    code = error.get("code")
    if code is not None:
        parts.append(f"code={code}")

    metadata = error.get("metadata")
    if isinstance(metadata, dict):
        provider = metadata.get("provider_name")
        if provider:
            parts.append(f"provider={provider}")
        upstream = _upstream_message(metadata.get("raw"))
        if upstream:
            parts.append(f"upstream: {upstream}")

    return " | ".join(parts)[:_DETAIL_LIMIT]


def _upstream_message(raw: object) -> str:
    """Pull the provider's own message out of ``metadata.raw``.

    The value arrives as a string, sometimes SSE-framed (``data: {...}``) and
    sometimes not JSON at all. A malformed one is truncated rather than raised
    on: this runs while reporting a failure, and a diagnostic helper that throws
    would replace a useful message with a confusing one.
    """
    if not isinstance(raw, str) or not raw.strip():
        return ""

    text = raw.strip()
    if text.startswith("data:"):
        text = text[len("data:") :].strip()

    try:
        decoded = json.loads(text)
    except ValueError:
        return text[:200]

    if isinstance(decoded, dict):
        inner = decoded.get("error")
        if isinstance(inner, dict):
            message = str(inner.get("message", "")).strip()
            inner_code = inner.get("code")
            if message and inner_code:
                return f"{message} ({inner_code})"
            if message:
                return message
    return text[:200]
