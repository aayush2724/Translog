"""EmailSink implementations for the demo.

Neither sends mail. `CollectingEmailSink` keeps messages in memory so a test or
a demo can assert on what would have gone out; `FileOutboxSink` additionally
writes each one to a directory so a person can read it.

A real SMTP or Gmail sink is later work behind the same port, and nothing here
pretends otherwise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from translog_quote.domain.email import OutboundMessage


class CollectingEmailSink:
    """Keeps every message it is given, in order."""

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)


class FileOutboxSink:
    """Collects, and also writes each message to a numbered file."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self.sent: list[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / f"{len(self.sent):03d}_clarification.txt"
        path.write_text(
            f"To: {message.to_address}\n"
            f"Subject: {message.subject}\n"
            f"In-Reply-To: {message.in_reply_to or '-'}\n\n"
            f"{message.body_text}\n",
            encoding="utf-8",
        )
