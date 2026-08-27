"""Splitting a thread export into messages, and telling client from Translog.

This is the safety-critical part of the evaluation. A Translog reply in the
same PDF often contains exactly the fields we are testing extraction of —
weight, dimensions, commodity — because Translog quotes them back. Feeding that
to the model would measure nothing except its ability to read our own words, so
sender attribution has to be right before any accuracy number means anything.

Deterministic throughout. No model is involved in deciding who said what.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

TRANSLOG_DOMAIN = "translogexpress.com"

_EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"

#: Gmail inline quote: "On Wed, Jul 22, 2026 at 4:40 PM <someone@x.com> wrote:"
_GMAIL_QUOTE = re.compile(rf"^On\s+.{{4,80}}?({_EMAIL})>?\s*wrote:\s*$", re.IGNORECASE)

#: Outlook forward block: "From: Mehul Darji [mailto:someone@x.com]" or "From: X <a@b.c>"
_FORWARD_FROM = re.compile(rf"^From:\s*.*?({_EMAIL})", re.IGNORECASE)

#: Gmail's own rendering of a message header, which carries a sender and a date
#: on one line: "Prachi Shah <p@translogexpress.com> Wed, Jul 22, 2026 at 5:16 PM"
_HEADER_WITH_DATE = re.compile(
    rf"^(?P<name>.*?)<(?P<email>{_EMAIL})>\s+"
    r"(?P<date>(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+.*\d{4}.*)$",
    re.IGNORECASE,
)

#: Lines that are addressing headers, not content.
_ADDRESS_HEADER = re.compile(r"^(To|Cc|Bcc|Sent|Subject|Date|Reply-To):", re.IGNORECASE)

#: The export preamble: "1 message", "3 messages".
_MESSAGE_COUNT = re.compile(r"^\d+\s+messages?$", re.IGNORECASE)


class SenderKind(StrEnum):
    CLIENT = "client"
    TRANSLOG = "translog"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Message:
    """One message in a thread, with who sent it and what they wrote."""

    order: int
    sender_email: str
    kind: SenderKind
    body: str

    @property
    def is_client(self) -> bool:
        return self.kind is SenderKind.CLIENT


def classify_sender(email: str) -> SenderKind:
    """Translog's own domain is the only thing that makes a message ours."""
    if not email:
        return SenderKind.UNKNOWN
    return (
        SenderKind.TRANSLOG
        if email.strip().lower().endswith(f"@{TRANSLOG_DOMAIN}")
        else SenderKind.CLIENT
    )


@dataclass(frozen=True, slots=True)
class _Boundary:
    line: int
    email: str


def _find_boundaries(lines: list[str]) -> list[_Boundary]:
    found: list[_Boundary] = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        for pattern in (_GMAIL_QUOTE, _FORWARD_FROM, _HEADER_WITH_DATE):
            match = pattern.match(line)
            if match:
                email = match.group("email") if "email" in match.groupdict() else match.group(1)
                found.append(_Boundary(line=i, email=email))
                break
    return found


def _clean_body(lines: list[str]) -> str:
    """Drop addressing headers and the export's message-count preamble."""
    kept = [
        line
        for line in lines
        if not (_ADDRESS_HEADER.match(line.strip()) or _MESSAGE_COUNT.match(line.strip()))
    ]
    return "\n".join(kept).strip()


def split_thread(text: str) -> tuple[Message, ...]:
    """Split cleaned thread text into attributed messages.

    Anything before the first boundary is the export's own preamble — mailbox
    owner and thread subject — and is discarded rather than guessed at. A thread
    with no recognisable boundary yields no messages, which the caller reports
    as a parsing failure instead of silently treating the whole file as one
    client email.
    """
    lines = text.splitlines()
    boundaries = _find_boundaries(lines)
    if not boundaries:
        return ()

    messages: list[Message] = []
    for order, boundary in enumerate(boundaries):
        start = boundary.line + 1
        end = boundaries[order + 1].line if order + 1 < len(boundaries) else len(lines)
        body = _clean_body(lines[start:end])
        if not body:
            continue
        messages.append(
            Message(
                order=order,
                sender_email=boundary.email,
                kind=classify_sender(boundary.email),
                body=body,
            )
        )
    return tuple(messages)


def client_messages(messages: tuple[Message, ...]) -> tuple[Message, ...]:
    """Only what the client wrote.

    Thread exports run newest-first, so these are returned in reverse order —
    oldest client message first, which is the order the real conversation
    happened in and the order a merge would apply them.
    """
    return tuple(reversed([m for m in messages if m.is_client]))


def client_request_text(messages: tuple[Message, ...]) -> str:
    """Every client message in one block, oldest first.

    This is what extraction sees. It contains no Translog message, so a rate or
    a restated weight from our own reply cannot leak in and be scored as if the
    client had said it.
    """
    return "\n\n---\n\n".join(m.body for m in client_messages(messages))
