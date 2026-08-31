"""GmailEmailSource — one real mailbox behind the existing ``EmailSource`` port.

Phase 10.3 scope: **receive only**. This module can list and read messages from
one configured test mailbox and nothing else. There is no send path, no draft
path, no label writing, no deletion — the only Gmail scope it operates under is
``gmail.readonly``, and every request it makes is an HTTP GET. The one POST in
the file goes to Google's fixed OAuth token endpoint to refresh an access
token, never to a mail endpoint.

Boundaries, same as every adapter here:

- It understands mail, not cargo: Gmail JSON in, ``RawEmail`` out. No shipment
  parsing, no model calls, no business rules.
- Provider failures are translated to the project taxonomy at this edge;
  no ``httpx`` exception and no Gmail-shaped error escapes ``adapters/``.
- Credentials are read from a git-ignored token file, held in memory, sent in
  an Authorization header, and never logged, echoed, or interpolated into any
  exception message.

Gmail's own message/thread ids do not belong on ``RawEmail`` (the domain type
is provider-agnostic, and the Phase 10.2 discovery settled that provider ids
are adapter metadata, not domain fields) — so the source keeps them in an
in-memory map, exposed via ``provider_metadata`` for later correlation work.
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parseaddr, parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Protocol

import httpx

# Deliberate reuse of the extraction transport's retry-policy pieces (cap and
# Retry-After parsing) rather than a second copy. If a third HTTP adapter ever
# appears, these graduate into a shared adapters/http module.
from translog_quote.adapters.extraction.transport import (
    MAX_RETRY_WAIT_SECONDS,
    _parse_retry_after,
)
from translog_quote.domain.email import RawEmail
from translog_quote.errors import ContractViolation, PermanentFailure, TransientFailure
from translog_quote.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Collection
    from pathlib import Path

_log = get_logger("adapters.email.gmail")

#: Fixed API endpoints. Constants on purpose: no configuration value, setting,
#: or message content can redirect a request anywhere else.
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

#: The one scope this integration is allowed to hold. Read-only by design:
#: with this scope the credential *cannot* send, modify, or delete anything,
#: whatever the code does.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

#: Gmail message ids as the API actually issues them. Anything else in an id
#: position is a malformed response and must not reach URL construction.
_GMAIL_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class _Throttled(TransientFailure):
    """Transient, and the provider said how long to wait."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class _Unauthorized(PermanentFailure):
    """The access token was rejected. Caught once for a single refresh; a
    second rejection escapes as the ``PermanentFailure`` it already is."""


class _NotFound(PermanentFailure):
    """A listed message no longer exists. Normal between list and get; the
    source skips it. Never escapes this module."""


@dataclass(frozen=True)
class _TokenFile:
    """The authorized-user credential, parsed. Fields are excluded from
    ``repr``, so the secret cannot leak through a stack trace or debug log."""

    refresh_token: str = field(repr=False)
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)


def _load_token_file(path: Path, *, command: str = "gmail-auth") -> _TokenFile:
    """Read one authorized-user token, or refuse with the command that makes it.

    ``command`` names the consent command that writes *this* token, because the
    two are not interchangeable: `gmail-auth` grants read and `gmail-auth-send`
    grants send, and a message naming the wrong one sends the operator to a
    grant that leaves the actual problem in place.

    The `--env-file` reminder is part of the instruction rather than a footnote.
    Consent writes to whichever token path the loaded configuration names, so
    running the bare command while a second account is configured writes the
    token to the *default* path — overwriting the first account's credential,
    which is the one thing a missing-token message must not talk somebody into.
    """
    if not path.exists():
        raise PermanentFailure(
            f"No Gmail token file at {path}. Grant consent for this account with the "
            "one-time command: python -m translog_quote.interface.demo "
            f"--env-file <the env file naming this token path> {command} "
            "(without the matching --env-file the token is written to the default "
            "path, overwriting another account's credential)."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # Deliberately no file content in the message — it holds credentials.
        raise PermanentFailure(f"Gmail token file at {path} could not be read as JSON") from exc

    if not isinstance(data, dict):
        raise PermanentFailure(f"Gmail token file at {path} is not a JSON object")

    values: dict[str, str] = {}
    missing: list[str] = []
    for key in ("refresh_token", "client_id", "client_secret"):
        value = data.get(key)
        if isinstance(value, str) and value:
            values[key] = value
        else:
            missing.append(key)
    if missing:
        raise PermanentFailure(
            f"Gmail token file at {path} is missing {missing}. "
            f"Re-run the {command} consent command."
        )

    return _TokenFile(
        refresh_token=values["refresh_token"],
        client_id=values["client_id"],
        client_secret=values["client_secret"],
    )


class GmailTransport(Protocol):
    """One authenticated GET against the Gmail REST API, decoded JSON back.

    A seam, so the source's mapping and filtering can be tested with canned
    dictionaries and no network. Implementations raise the project taxonomy
    and nothing else.
    """

    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]: ...


class HttpxGmailTransport:
    """The real transport. GETs only, against ``GMAIL_API_BASE``.

    Token lifecycle: the refresh token from the git-ignored token file buys an
    access token at first use; a 401 mid-run buys exactly one more refresh and
    one retry, then stops — never a loop. The token endpoint is the pinned
    ``GOOGLE_TOKEN_URL`` constant; any ``token_uri`` in the token file is
    ignored, so no file edit can redirect credentials elsewhere.
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
        # Fail at construction, not halfway through a fetch.
        self._token_file = _load_token_file(token_path)
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff = max(0.0, backoff_seconds)
        self._client = client
        self._sleep = sleep
        self._jitter = jitter
        self._access_token: str | None = None

    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            return self._request_with_retries(path, params)
        except _Unauthorized:
            # One refresh, one retry. Per the Gmail error guidance: an invalid
            # credential is refreshed once, never retried in a loop.
            _log.info("Gmail access token rejected; refreshing once")
            self._access_token = None
            return self._request_with_retries(path, params)

    def _request_with_retries(self, path: str, params: dict[str, str] | None) -> dict[str, Any]:
        attempts = self._max_retries + 1
        last_transient: TransientFailure | None = None

        for attempt in range(1, attempts + 1):
            try:
                return self._attempt(path, params)
            except TransientFailure as exc:
                last_transient = exc
                _log.warning("Gmail request failed (attempt %d/%d): %s", attempt, attempts, exc)
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

    def _attempt(self, path: str, params: dict[str, str] | None) -> dict[str, Any]:
        url = f"{GMAIL_API_BASE}/{path}"
        headers = {"Authorization": f"Bearer {self._ensure_access_token()}"}
        try:
            if self._client is not None:
                response = self._client.get(
                    url, params=params, headers=headers, timeout=self._timeout
                )
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise TransientFailure(f"Gmail request timed out after {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            # Type name only: httpx exception text can include the URL, and
            # keeping credentials out of URLs is a rule worth double-locking.
            raise TransientFailure(
                f"Gmail request failed at the transport layer: {type(exc).__name__}"
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
            raise TransientFailure("Gmail token refresh timed out") from exc
        except httpx.HTTPError as exc:
            raise TransientFailure(
                f"Gmail token refresh failed at the transport layer: {type(exc).__name__}"
            ) from exc

        if response.status_code != 200:
            # The OAuth error *code* (e.g. invalid_grant) is diagnostic and
            # safe; nothing else from the exchange is repeated anywhere.
            raise PermanentFailure(
                f"Gmail token refresh was rejected ({response.status_code}: "
                f"{_oauth_error_code(response)}). Re-run the gmail-auth consent command."
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise PermanentFailure("Gmail token refresh returned a non-JSON body") from exc
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise PermanentFailure("Gmail token refresh response carried no access token")
        return token

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        status = response.status_code

        if status in _RETRYABLE_STATUS:
            raise _Throttled(
                f"Gmail API returned {status}: {_gmail_detail(response)}",
                retry_after=_parse_retry_after(response.headers.get("Retry-After")),
            )
        if status == 401:
            raise _Unauthorized(
                "Gmail API rejected the credentials (401). Re-run the gmail-auth consent command."
            )
        if status == 403:
            raise PermanentFailure(
                f"Gmail API refused the request (403): {_gmail_detail(response)}. "
                "Check that consent granted the gmail.readonly scope."
            )
        if status == 404:
            raise _NotFound(f"Gmail API returned 404: {_gmail_detail(response)}")
        if status == 400:
            raise ContractViolation(
                f"Gmail API rejected the request (400): {_gmail_detail(response)}"
            )
        if status >= 400:
            raise PermanentFailure(f"Gmail API returned {status}: {_gmail_detail(response)}")

        try:
            body = response.json()
        except ValueError as exc:
            raise ContractViolation("Gmail API returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise ContractViolation(
                f"Gmail API returned {type(body).__name__}, expected a JSON object"
            )
        return body


_DETAIL_LIMIT = 200


def _gmail_detail(response: httpx.Response) -> str:
    """A short, bounded description of a Gmail error body.

    Reads only the response's own ``error.message``/``error.status`` — never
    the request, its headers, or anything that could carry a token.
    """
    try:
        payload = response.json()
    except ValueError:
        return response.text[:_DETAIL_LIMIT]
    if not isinstance(payload, dict):
        return str(payload)[:_DETAIL_LIMIT]
    error = payload.get("error")
    if not isinstance(error, dict):
        return str(payload)[:_DETAIL_LIMIT]
    parts = [str(error.get("message", "")).strip() or "unspecified error"]
    if error.get("status"):
        parts.append(f"status={error['status']}")
    return " | ".join(parts)[:_DETAIL_LIMIT]


def _oauth_error_code(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "unreadable error"
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return str(payload["error"])[:50]
    return "unspecified error"


# --- Gmail message → RawEmail ---------------------------------------------------


def _decode_body_data(data: str) -> str:
    """Gmail body data is base64url. Malformed data is a broken response, not
    something to repair by guessing."""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ContractViolation("Gmail message body is not valid base64url") from exc
    return raw.decode("utf-8", errors="replace")


def _collect_text_parts(part: dict[str, Any], mime_prefix: str) -> list[str]:
    """Depth-first walk of the MIME part tree, collecting decoded bodies of
    the given ``text/...`` type."""
    found: list[str] = []
    mime_type = part.get("mimeType", "")
    body = part.get("body")
    data = body.get("data") if isinstance(body, dict) else None
    if (
        isinstance(mime_type, str)
        and mime_type.lower().startswith(mime_prefix)
        and isinstance(data, str)
        and data
    ):
        found.append(_decode_body_data(data))
    parts = part.get("parts")
    if isinstance(parts, list):
        for child in parts:
            if isinstance(child, dict):
                found.extend(_collect_text_parts(child, mime_prefix))
    return found


_TAG_STRIP = re.compile(r"<[^>]+>")
_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_BLOCK_BREAK = re.compile(r"<\s*(?:br\s*/?|/p|/div|/tr|/li)\s*>", re.IGNORECASE)


def _html_to_text(markup: str) -> str:
    """Strip an HTML-only message down to plain text.

    HTML from a mailbox is untrusted input: it is never rendered or
    interpreted, only reduced to text for the same pipeline every plain-text
    email goes through.
    """
    text = _SCRIPT_STYLE.sub(" ", markup)
    text = _BLOCK_BREAK.sub("\n", text)
    text = _TAG_STRIP.sub(" ", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _extract_body_text(payload: dict[str, Any]) -> str:
    """The message's readable text: ``text/plain`` preferred, stripped
    ``text/html`` as the fallback."""
    plain = _collect_text_parts(payload, "text/plain")
    if plain:
        return "\n".join(plain).strip("\n")
    html_parts = _collect_text_parts(payload, "text/html")
    if html_parts:
        return _html_to_text("\n".join(html_parts))
    raise ContractViolation("Gmail message has no readable text part")


def _header_map(payload: dict[str, Any]) -> dict[str, str]:
    headers = payload.get("headers")
    if not isinstance(headers, list):
        raise ContractViolation("Gmail message payload has no headers list")
    mapping: dict[str, str] = {}
    for entry in headers:
        if not isinstance(entry, dict):
            continue
        name, value = entry.get("name"), entry.get("value")
        if isinstance(name, str) and isinstance(value, str):
            mapping.setdefault(name.lower(), value)
    return mapping


def _received_at(headers: dict[str, str], message: dict[str, Any]) -> datetime:
    """The ``Date`` header when it parses; Gmail's ``internalDate`` otherwise."""
    raw_date = headers.get("date")
    if raw_date:
        try:
            return parsedate_to_datetime(raw_date)
        except (TypeError, ValueError):
            pass
    internal = message.get("internalDate")
    if isinstance(internal, str) and internal.isdigit():
        return datetime.fromtimestamp(int(internal) / 1000, tz=UTC)
    raise ContractViolation("Gmail message carries no parseable date")


def parse_gmail_message(message: dict[str, Any]) -> RawEmail:
    """One ``messages.get(format=full)`` response body → ``RawEmail``.

    The RFC 5322 ``Message-ID`` is the canonical id (it is what
    ``in_reply_to``/``references`` chains point at); Gmail's own id fills in
    only when a message somehow lacks one.
    """
    payload = message.get("payload")
    if not isinstance(payload, dict):
        raise ContractViolation("Gmail message has no payload")

    headers = _header_map(payload)

    # `parseaddr` is lenient — it hands back bare text unchanged rather than
    # failing — so the "@" check is what actually rejects a broken From.
    from_address = parseaddr(headers.get("from", ""))[1]
    if "@" not in from_address:
        raise ContractViolation("Gmail message has no parseable From address")

    gmail_id = message.get("id")
    message_id = headers.get("message-id") or (gmail_id if isinstance(gmail_id, str) else None)
    if not message_id:
        raise ContractViolation("Gmail message has neither a Message-ID header nor an id")

    references = tuple(headers["references"].split()) if headers.get("references") else ()

    return RawEmail(
        message_id=message_id,
        from_address=from_address,
        subject=headers.get("subject", ""),
        body_text=_extract_body_text(payload),
        received_at=_received_at(headers, message),
        in_reply_to=headers.get("in-reply-to") or None,
        references=references,
    )


@dataclass(frozen=True)
class GmailMessageMetadata:
    """Provider-side identifiers for one ingested message. Adapter metadata,
    deliberately not a ``RawEmail`` field (Phase 10.2 discovery, §F)."""

    gmail_id: str
    thread_id: str | None


class GmailEmailSource:
    """An ``EmailSource`` over one narrowly scoped slice of one test mailbox.

    Safety posture for the first real test:

    - refuses to run unless the authorized account is exactly the configured
      test address (a token for any other mailbox is rejected);
    - reads only what ``query`` matches (default ``in:inbox``), at most
      ``max_results`` messages (default 1) — never the whole mailbox;
    - skips sent-only and draft messages defensively, on top of the query;
    - retrieves; it cannot send, modify, or delete (GET-only transport, and
      the requested OAuth scope is ``gmail.readonly``).
    """

    def __init__(
        self,
        transport: GmailTransport,
        *,
        mailbox_address: str,
        query: str = "in:inbox",
        max_results: int = 1,
        sent_by_us: Callable[[], Collection[str]] | None = None,
    ) -> None:
        if not mailbox_address:
            raise PermanentFailure(
                "No Gmail test mailbox configured. Set TRANSLOG_GMAIL__TEST_ADDRESS."
            )
        self._transport = transport
        self._mailbox_address = mailbox_address
        self._query = query
        self._max_results = max_results
        self._sent_by_us_ids = sent_by_us
        self._metadata: dict[str, GmailMessageMetadata] = {}

    def provider_metadata(self, message_id: str) -> GmailMessageMetadata | None:
        """Gmail's own ids for a message this source returned, if any."""
        return self._metadata.get(message_id)

    def fetch_new(self) -> tuple[RawEmail, ...]:
        self._verify_mailbox()

        listing = self._transport.get_json(
            "messages", {"q": self._query, "maxResults": str(self._max_results)}
        )
        refs = listing.get("messages")
        if refs is None:
            return ()  # an empty mailbox slice lists no "messages" key at all
        if not isinstance(refs, list):
            raise ContractViolation("Gmail messages.list response is malformed")

        emails: list[RawEmail] = []
        for ref in refs[: self._max_results]:
            gmail_id = ref.get("id") if isinstance(ref, dict) else None
            if not isinstance(gmail_id, str) or not _GMAIL_ID.match(gmail_id):
                raise ContractViolation("Gmail messages.list entry has no usable id")

            try:
                full = self._transport.get_json(f"messages/{gmail_id}", {"format": "full"})
            except _NotFound:
                # Deleted between list and get. Normal, not an error.
                _log.info("Gmail message %s vanished between list and get; skipped", gmail_id)
                continue

            if self._is_own_outbound(full):
                _log.info("Gmail message %s is sent/draft mail; skipped", gmail_id)
                continue

            if self._sent_by_us(gmail_id):
                # A message this run delivered, arriving in our own inbox
                # because Translog reads and sends from one mailbox. It is our
                # words, not a client's, and reading it back as an inbound
                # message corrupts the request it correlates to.
                _log.info("Gmail message %s was sent by this run; skipped", gmail_id)
                continue

            raw = parse_gmail_message(full)
            thread_id = full.get("threadId")
            self._metadata[raw.message_id] = GmailMessageMetadata(
                gmail_id=gmail_id,
                thread_id=thread_id if isinstance(thread_id, str) else None,
            )
            emails.append(raw)

        return tuple(emails)

    def _verify_mailbox(self) -> None:
        """Refuse to read anything if the token belongs to another account.

        This is the guard that keeps "the company mailbox is NOT connected"
        structural: even a valid token for the wrong mailbox stops here.
        """
        profile = self._transport.get_json("profile")
        actual = profile.get("emailAddress")
        if not isinstance(actual, str) or actual.strip().lower() != self._mailbox_address.lower():
            # The authorized account's address is deliberately not echoed.
            raise PermanentFailure(
                "The authorized Gmail account does not match TRANSLOG_GMAIL__TEST_ADDRESS. "
                "Refusing to read this mailbox."
            )

    def _sent_by_us(self, gmail_id: str) -> bool:
        """Whether this run delivered this exact message.

        Asked as a callable rather than handed a snapshot: the set grows every
        time a clarification or quotation goes out, and a source built at the
        start of a poll must see what was sent since.
        """
        if self._sent_by_us_ids is None:
            return False
        return gmail_id in self._sent_by_us_ids()

    @staticmethod
    def _is_own_outbound(message: dict[str, Any]) -> bool:
        """Sent-only or draft mail is never ingested.

        A message that is in the inbox *and* sent (a self-addressed test
        email) still passes — that is exactly the Phase 10.3 test shape.
        """
        labels = message.get("labelIds")
        label_set = (
            {label for label in labels if isinstance(label, str)}
            if isinstance(labels, list)
            else set()
        )
        if "DRAFT" in label_set:
            return True
        return "SENT" in label_set and "INBOX" not in label_set
