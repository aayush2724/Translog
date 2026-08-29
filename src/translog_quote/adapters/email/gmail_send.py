"""GmailEmailSink — the outbound half of the Gmail integration.

Deliberately a **separate module from `gmail.py`**, and deliberately a separate
credential. The inbound source holds a token scoped to ``gmail.readonly`` and
issues only GETs; this module holds a token scoped to ``gmail.send`` and issues
only one POST, to one endpoint. Neither credential can do the other's job:

    inbound   gmail.readonly   GET  messages, messages/{id}, profile
    outbound  gmail.send       POST messages/send

That separation is a property of the OAuth grants, not of the code — so
"inbound processing is separate from the outbound sink" survives any future
edit to either file.

Boundaries, same as every adapter here:

- It understands mail, not cargo. ``OutboundMessage`` in, RFC 5322 out. It
  composes no copy of its own: every byte of subject and body was written
  deterministically by `domain`, and this module only frames it.
- Provider failures are translated to the project taxonomy at this edge.
- Credentials are read from a git-ignored token file, held in memory, sent in
  an Authorization header, and never logged or interpolated into an exception.
- It decides nothing about *whether* to send. Reaching this class already means
  a person approved, because the only callers sit behind the approval gate.
"""

from __future__ import annotations

import base64
import random
import time
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any, Protocol

import httpx

# Reuse of the inbound adapter's credential parsing and error decoding rather
# than a second copy of either. These are pure helpers over a token file and an
# HTTP response — sharing them couples the two modules to Google's wire format,
# which is the one thing they genuinely have in common, and to nothing else.
from translog_quote.adapters.email.gmail import (
    _RETRYABLE_STATUS,
    GMAIL_API_BASE,
    GOOGLE_TOKEN_URL,
    _gmail_detail,
    _load_token_file,
    _oauth_error_code,
    _Throttled,
    _Unauthorized,
)
from translog_quote.adapters.extraction.transport import (
    MAX_RETRY_WAIT_SECONDS,
    _parse_retry_after,
)
from translog_quote.errors import ContractViolation, PermanentFailure, TransientFailure
from translog_quote.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from translog_quote.domain.email import OutboundMessage

_log = get_logger("adapters.email.gmail_send")

#: The one scope this half of the integration is allowed to hold. Send-only by
#: design: with this scope the credential *cannot* read, list, label, or delete
#: anything in the mailbox, whatever the code does.
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

#: The single endpoint this module posts to. A constant, so no setting and no
#: message content can redirect a send anywhere else.
SEND_PATH = "messages/send"


class GmailSendTransport(Protocol):
    """One authenticated POST against the Gmail send endpoint, JSON back.

    A seam, so the sink's MIME framing can be tested without a network and
    without a credential. Implementations raise the project taxonomy and
    nothing else.
    """

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class HttpxGmailSendTransport:
    """The real transport. POSTs only, to ``GMAIL_API_BASE/messages/send``.

    Token lifecycle mirrors the inbound transport: the refresh token from the
    git-ignored send token file buys an access token at first use; a 401
    mid-run buys exactly one more refresh and one retry, then stops. The token
    endpoint is the pinned ``GOOGLE_TOKEN_URL`` constant, so no file edit can
    redirect credentials elsewhere.

    Retries are bounded and apply only to transient statuses. A send that
    failed permanently is never retried: mailing a client twice is worse than
    not mailing them at all.
    """

    def __init__(
        self,
        *,
        token_path: Path,
        timeout_seconds: int,
        max_retries: int,
        backoff_seconds: float = 1.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        # Fail at construction, not halfway through a demo.
        self._token_file = _load_token_file(token_path)
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff = max(0.0, backoff_seconds)
        self._client = client
        self._sleep = sleep
        self._jitter = jitter
        self._access_token: str | None = None

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path != SEND_PATH:
            # There is exactly one endpoint this transport is for. Anything
            # else is a programming error, caught here rather than discovered
            # as a surprising HTTP call.
            raise ContractViolation(f"Gmail send transport may only post to {SEND_PATH!r}")
        try:
            return self._request_with_retries(path, payload)
        except _Unauthorized:
            _log.info("Gmail send token rejected; refreshing once")
            self._access_token = None
            return self._request_with_retries(path, payload)

    def _request_with_retries(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        attempts = self._max_retries + 1
        last_transient: TransientFailure | None = None

        for attempt in range(1, attempts + 1):
            try:
                return self._attempt(path, payload)
            except TransientFailure as exc:
                last_transient = exc
                _log.warning("Gmail send failed (attempt %d/%d): %s", attempt, attempts, exc)
                if attempt < attempts:
                    self._sleep(self._delay_before_retry(attempt, exc))

        assert last_transient is not None  # only reachable via the except branch
        raise last_transient

    def _delay_before_retry(self, attempt: int, exc: TransientFailure) -> float:
        if isinstance(exc, _Throttled) and exc.retry_after is not None:
            return min(exc.retry_after, MAX_RETRY_WAIT_SECONDS)
        base: float = self._backoff * float(2 ** (attempt - 1))
        spread: float = self._jitter() * self._backoff
        return min(base + spread, MAX_RETRY_WAIT_SECONDS)

    def _attempt(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{GMAIL_API_BASE}/{path}"
        headers = {"Authorization": f"Bearer {self._ensure_access_token()}"}
        try:
            if self._client is not None:
                response = self._client.post(
                    url, json=payload, headers=headers, timeout=self._timeout
                )
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise TransientFailure(f"Gmail send timed out after {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            # Type name only: httpx exception text can include the URL.
            raise TransientFailure(
                f"Gmail send failed at the transport layer: {type(exc).__name__}"
            ) from exc

        return self._decode(response)

    def _ensure_access_token(self) -> str:
        if self._access_token is None:
            self._access_token = self._refresh_access_token()
        return self._access_token

    def _refresh_access_token(self) -> str:
        payload = {
            "client_id": self._token_file.client_id,
            "client_secret": self._token_file.client_secret,
            "refresh_token": self._token_file.refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            if self._client is not None:
                response = self._client.post(GOOGLE_TOKEN_URL, data=payload, timeout=self._timeout)
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(GOOGLE_TOKEN_URL, data=payload)
        except httpx.TimeoutException as exc:
            raise TransientFailure("Gmail send token refresh timed out") from exc
        except httpx.HTTPError as exc:
            raise TransientFailure(
                f"Gmail send token refresh failed at the transport layer: {type(exc).__name__}"
            ) from exc

        if response.status_code != 200:
            raise PermanentFailure(
                f"Gmail send token refresh was rejected ({response.status_code}: "
                f"{_oauth_error_code(response)}). Re-run the gmail-auth-send consent command."
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise PermanentFailure("Gmail send token refresh returned a non-JSON body") from exc
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise PermanentFailure("Gmail send token refresh response carried no access token")
        return token

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        status = response.status_code

        if status in _RETRYABLE_STATUS:
            raise _Throttled(
                f"Gmail send returned {status}: {_gmail_detail(response)}",
                retry_after=_parse_retry_after(response.headers.get("Retry-After")),
            )
        if status == 401:
            raise _Unauthorized(
                "Gmail rejected the send credentials (401). "
                "Re-run the gmail-auth-send consent command."
            )
        if status == 403:
            raise PermanentFailure(
                f"Gmail refused the send (403): {_gmail_detail(response)}. "
                "Check that consent granted the gmail.send scope, and that the From "
                "address is the authenticated account."
            )
        if status == 400:
            raise ContractViolation(f"Gmail rejected the send (400): {_gmail_detail(response)}")
        if status >= 400:
            raise PermanentFailure(f"Gmail send returned {status}: {_gmail_detail(response)}")

        try:
            body = response.json()
        except ValueError as exc:
            raise ContractViolation("Gmail send returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise ContractViolation(
                f"Gmail send returned {type(body).__name__}, expected a JSON object"
            )
        return body


# --- OutboundMessage → RFC 5322 -------------------------------------------------


def _header_safe(name: str, value: str) -> str:
    """Reject a header value carrying a line break.

    Subjects and addresses on an outbound message can originate in an inbound
    email — a reply subject is the client's own text with "Re: " prefixed. A CR
    or LF in a header value is header injection, and the correct response is to
    refuse to build the message rather than to strip characters and send
    something the caller did not write.
    """
    if "\r" in value or "\n" in value:
        raise ContractViolation(f"{name} header contains a line break; refusing to send")
    return value


def build_mime(message: OutboundMessage, *, sender_address: str) -> str:
    """One ``OutboundMessage`` as a base64url-encoded RFC 5322 message.

    ``In-Reply-To`` and ``References`` are both set from the message's parent
    id when it has one, which is what places a reply in the client's existing
    thread rather than starting a new conversation beside it.
    """
    mime = EmailMessage()
    mime["To"] = _header_safe("To", message.to_address)
    mime["From"] = _header_safe("From", sender_address)
    mime["Subject"] = _header_safe("Subject", message.subject)
    if message.in_reply_to:
        parent = _header_safe("In-Reply-To", message.in_reply_to)
        mime["In-Reply-To"] = parent
        mime["References"] = parent
    mime.set_content(message.body_text)

    return base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")


class GmailEmailSink:
    """An ``EmailSink`` that actually delivers, over one Gmail account.

    The `From` address is required and is written into every message. Gmail
    validates it against the authenticated account and rejects a mismatch — so
    a token for the wrong mailbox fails at the provider rather than quietly
    sending as someone else. That is the outbound counterpart to the inbound
    source's mailbox check.

    It keeps what it sent in ``sent``, in order, exactly as the collecting sink
    does. A demo needs to show what went out, and reading it back from Gmail
    would need a read scope this credential deliberately does not hold.
    """

    def __init__(self, transport: GmailSendTransport, *, sender_address: str) -> None:
        if not sender_address:
            raise PermanentFailure(
                "No Gmail sender address configured. Set TRANSLOG_GMAIL__SENDER_ADDRESS."
            )
        self._transport = transport
        self._sender_address = sender_address
        self.sent: list[OutboundMessage] = []

    @property
    def sender_address(self) -> str:
        return self._sender_address

    def send(self, message: OutboundMessage) -> None:
        raw = build_mime(message, sender_address=self._sender_address)
        self._transport.post_json(SEND_PATH, {"raw": raw})
        # Appended only after the provider accepted it. A failed send must not
        # look like a delivered one in the run's own record of what went out.
        self.sent.append(message)
        # Recipient and subject only. Never the body — it is client
        # correspondence — and never anything from the credential.
        _log.info("Gmail send accepted: to=%s subject=%r", message.to_address, message.subject)
