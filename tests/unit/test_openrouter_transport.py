"""The HTTP transport, exercised against a mock server rather than the network.

`httpx.MockTransport` intercepts requests inside httpx itself, so these tests
assert real request construction and real status handling without a socket or
an API key.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from translog_quote.adapters.extraction import HttpxChatTransport
from translog_quote.adapters.extraction.transport import _DETAIL_LIMIT
from translog_quote.errors import PermanentFailure, TransientFailure

#: Stand-in credential. Deliberately not shaped like a real provider key, so
#: secret scanners do not raise an incident on an obviously fake one.
API_KEY = "test-not-a-real-credential"
PAYLOAD: dict[str, Any] = {"model": "qwen/qwen3.7-flash", "messages": []}


def transport_with(handler: Any, *, max_retries: int = 0) -> HttpxChatTransport:
    return HttpxChatTransport(
        api_key=API_KEY,
        base_url="https://openrouter.ai/api/v1",
        timeout_seconds=5,
        max_retries=max_retries,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


# --- request construction ------------------------------------------------------


def test_it_posts_to_the_chat_completions_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": []})

    transport_with(handler).post_chat_completion(PAYLOAD)

    assert str(seen[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert seen[0].method == "POST"


def test_a_trailing_slash_in_the_base_url_does_not_double_up() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    HttpxChatTransport(
        api_key=API_KEY,
        base_url="https://openrouter.ai/api/v1/",
        timeout_seconds=5,
        max_retries=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).post_chat_completion(PAYLOAD)

    assert "//chat" not in str(seen[0].url).replace("https://", "")


def test_the_key_travels_in_the_authorization_header_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    transport_with(handler).post_chat_completion(PAYLOAD)

    request = seen[0]
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    # Never in the URL, where it would land in logs and proxy records.
    assert API_KEY not in str(request.url)
    assert API_KEY not in request.content.decode()


def test_a_missing_key_is_refused_at_construction() -> None:
    with pytest.raises(PermanentFailure, match="No OpenRouter API key"):
        HttpxChatTransport(api_key="", base_url="https://x", timeout_seconds=5, max_retries=0)


# --- status handling ------------------------------------------------------------


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_retryable_statuses_raise_transient(status: int) -> None:
    transport = transport_with(lambda request: httpx.Response(status, json={}))

    with pytest.raises(TransientFailure, match=str(status)):
        transport.post_chat_completion(PAYLOAD)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_permanent_and_name_the_setting(status: int) -> None:
    transport = transport_with(lambda request: httpx.Response(status, json={}))

    with pytest.raises(PermanentFailure, match="TRANSLOG_OPENROUTER__API_KEY"):
        transport.post_chat_completion(PAYLOAD)


def test_a_bad_request_is_permanent_and_carries_the_provider_message() -> None:
    """A 400 naming an unknown model is the exact failure the brief said to
    report rather than work around, so the message has to survive."""
    transport = transport_with(
        lambda request: httpx.Response(
            400, json={"error": {"message": "qwen/nonexistent is not a valid model ID"}}
        )
    )

    with pytest.raises(PermanentFailure, match="not a valid model ID"):
        transport.post_chat_completion(PAYLOAD)


def test_an_error_body_excerpt_is_bounded() -> None:
    """A provider can return a very large body; the exception must not."""
    transport = transport_with(
        lambda request: httpx.Response(400, json={"error": {"message": "x" * 5000}})
    )

    with pytest.raises(PermanentFailure) as excinfo:
        transport.post_chat_completion(PAYLOAD)

    assert len(str(excinfo.value)) <= _DETAIL_LIMIT + 64  # + the "OpenRouter returned 400: " prefix


def test_a_non_json_body_is_permanent() -> None:
    transport = transport_with(lambda request: httpx.Response(200, text="<html>gateway</html>"))

    with pytest.raises(PermanentFailure, match="non-JSON body"):
        transport.post_chat_completion(PAYLOAD)


def test_a_json_array_body_is_permanent() -> None:
    transport = transport_with(lambda request: httpx.Response(200, json=[1, 2, 3]))

    with pytest.raises(PermanentFailure, match="expected a JSON object"):
        transport.post_chat_completion(PAYLOAD)


# --- retries -----------------------------------------------------------------------


def test_a_transient_failure_is_retried_up_to_the_configured_bound() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503, json={})

    with pytest.raises(TransientFailure):
        transport_with(handler, max_retries=2).post_chat_completion(PAYLOAD)

    assert len(attempts) == 3  # the initial attempt plus two retries


def test_a_retry_that_succeeds_returns_the_body() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    body = transport_with(handler, max_retries=2).post_chat_completion(PAYLOAD)

    assert len(attempts) == 2
    assert "choices" in body


def test_a_permanent_failure_is_not_retried() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, json={})

    with pytest.raises(PermanentFailure):
        transport_with(handler, max_retries=3).post_chat_completion(PAYLOAD)

    assert len(attempts) == 1


def test_a_timeout_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(TransientFailure, match="timed out"):
        transport_with(handler).post_chat_completion(PAYLOAD)


def test_a_connection_error_is_transient_and_leaks_no_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(TransientFailure) as excinfo:
        transport_with(handler).post_chat_completion(PAYLOAD)

    assert API_KEY not in str(excinfo.value)


# --- no httpx type escapes ------------------------------------------------------


def test_no_httpx_exception_escapes_the_transport() -> None:
    """Everything above `adapters/` sees only the project's own taxonomy."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(Exception) as excinfo:
        transport_with(handler).post_chat_completion(PAYLOAD)

    assert not isinstance(excinfo.value, httpx.HTTPError)


# --- provider error unwrapping -----------------------------------------------


def test_an_openrouter_wrapped_provider_error_is_unwrapped() -> None:
    """OpenRouter reports its own flat "Provider returned error" and hides the
    provider's real complaint in metadata.raw as an SSE-framed JSON string.
    Reporting only the outer message throws away the one sentence that says
    what is wrong — which is exactly what happened on the first live run."""
    body = {
        "error": {
            "message": "Provider returned error",
            "code": 400,
            "metadata": {
                "provider_name": "Alibaba",
                "raw": 'data: {"error":{"code":"invalid_parameter_error",'
                "\"message\":\"'messages' must contain the word 'json' in some form, "
                "to use 'response_format' of type 'json_object'.\"}}\n\n",
            },
        }
    }
    transport = transport_with(lambda request: httpx.Response(400, json=body))

    with pytest.raises(PermanentFailure) as excinfo:
        transport.post_chat_completion(PAYLOAD)

    message = str(excinfo.value)
    assert "must contain the word 'json'" in message  # the actionable part
    assert "provider=Alibaba" in message
    assert "code=400" in message


def test_a_plain_provider_error_without_metadata_still_reports() -> None:
    body = {"error": {"message": "qwen/nonexistent is not a valid model ID", "code": 400}}
    transport = transport_with(lambda request: httpx.Response(400, json=body))

    with pytest.raises(PermanentFailure, match="not a valid model ID"):
        transport.post_chat_completion(PAYLOAD)


def test_malformed_metadata_raw_does_not_break_error_reporting() -> None:
    """A diagnostic helper that throws would replace a useful message with a
    confusing one."""
    body = {
        "error": {
            "message": "Provider returned error",
            "code": 400,
            "metadata": {"provider_name": "Alibaba", "raw": "not json at all {{{"},
        }
    }
    transport = transport_with(lambda request: httpx.Response(400, json=body))

    with pytest.raises(PermanentFailure, match="Provider returned error"):
        transport.post_chat_completion(PAYLOAD)


def test_the_unwrapped_detail_is_still_bounded() -> None:
    body = {
        "error": {
            "message": "Provider returned error",
            "code": 400,
            "metadata": {
                "provider_name": "P",
                "raw": 'data: {"error":{"message":"' + "x" * 5000 + '"}}',
            },
        }
    }
    transport = transport_with(lambda request: httpx.Response(400, json=body))

    with pytest.raises(PermanentFailure) as excinfo:
        transport.post_chat_completion(PAYLOAD)

    assert len(str(excinfo.value)) <= _DETAIL_LIMIT + 64
