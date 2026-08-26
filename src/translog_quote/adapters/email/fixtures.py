"""FixtureEmailSource — the demo's deterministic ``EmailSource``.

Reads plain-text ``.eml``-style files from a directory and turns each into a
``RawEmail``. This is a **demo fixture adapter**, not a mail client: it does not
speak IMAP, POP or the Gmail API, and it must never be mistaken for one. A real
mailbox adapter (Gmail or otherwise) is later work behind the same
``EmailSource`` port — this file has nothing to do with building it.

The fixture format is a deliberately small header block (``Key: value`` lines)
followed by a blank line and a free-text body — just enough to carry what
``RawEmail`` needs, written so a human can read and edit the email like the
plain text it is. It is not RFC 5322/MIME, and no MIME library is used to read
it: parsing eleven header lines does not justify that dependency.

This module has exactly one job: turning fixture files into ``RawEmail``
values. It does not parse shipment fields, does not validate anything, and does
not decide which email belongs to which thread — thread correlation is
``domain.conversation``'s job (Phase 6), not this adapter's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from translog_quote.domain.conversation import Thread
from translog_quote.domain.email import RawEmail

if TYPE_CHECKING:
    from pathlib import Path

_HEADER_LINE = re.compile(r"^([A-Za-z][A-Za-z-]*):[ \t]?(.*)$")

_REQUIRED_HEADERS = ("message-id", "from", "subject", "date")


def parse_fixture_email(text: str) -> RawEmail:
    """Parse one fixture file's contents into a ``RawEmail``.

    Unknown headers (``To:`` is written into every fixture for realism, but
    ``RawEmail`` has no field for it) are read and silently dropped — the
    fixture file is allowed to look like a fuller email than the domain type
    captures, the same way a real ``.eml`` carries headers no system reads.
    """
    header_block, separator, body = text.partition("\n\n")
    if not separator:
        raise ValueError("fixture email has no blank line separating headers from body")

    headers: dict[str, str] = {}
    for line in header_block.splitlines():
        if not line.strip():
            continue
        match = _HEADER_LINE.match(line)
        if match is None:
            raise ValueError(f"malformed fixture header line: {line!r}")
        headers[match.group(1).lower()] = match.group(2).strip()

    missing = [name for name in _REQUIRED_HEADERS if name not in headers]
    if missing:
        raise ValueError(f"fixture email is missing required header(s): {missing}")

    references = tuple(headers["references"].split()) if "references" in headers else ()

    return RawEmail(
        message_id=headers["message-id"],
        from_address=headers["from"],
        subject=headers["subject"],
        body_text=body.strip("\n"),
        received_at=datetime.fromisoformat(headers["date"]),
        in_reply_to=headers.get("in-reply-to") or None,
        references=references,
    )


def load_fixture_emails(directory: Path) -> tuple[RawEmail, ...]:
    """Load every ``*.eml`` fixture in ``directory``, in filename order.

    Filename order is the fixture's arrival order — scenario files are named
    ``001_initial.eml``, ``002_reply.eml`` and so on precisely so that sorting
    by name reproduces chronological order without depending on filesystem
    metadata (mtime is not deterministic across a checkout or a CI runner).
    """
    return tuple(
        parse_fixture_email(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.eml"))
    )


class FixtureEmailSource:
    """An ``EmailSource`` backed by one directory of fixture files.

    Returns every fixture message in the directory on every call — there is no
    polling state, no "already seen" tracking, and no persistence. Deciding
    what has already been processed is the pipeline's job (Phase 6, via
    ``StorePort``), not this adapter's: giving it that responsibility now would
    be exactly the persistence over-engineering this phase was told to avoid.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def fetch_new(self) -> tuple[RawEmail, ...]:
        return load_fixture_emails(self._directory)


@dataclass(frozen=True, slots=True)
class EmailFixtureScenario:
    """One named fixture scenario: a directory's messages, and the thread they
    are known to belong to.

    ``request_id`` is fixture-assigned metadata, not the output of a
    correlation algorithm — ``domain.conversation.CorrelationPolicy`` has no
    concrete implementation yet (Phase 6), so there is nothing to invoke here.
    What this phase can and does guarantee is that the fixture *data* is
    correlatable: each reply's ``in_reply_to``/``references`` correctly chains
    to the message before it, which is the precondition a real policy will
    depend on later.
    """

    name: str
    request_id: str
    messages: tuple[RawEmail, ...]

    @property
    def thread(self) -> Thread:
        return Thread(
            request_id=self.request_id,
            message_ids=tuple(message.message_id for message in self.messages),
        )


_SCENARIO_REQUEST_IDS: dict[str, str] = {
    "a_complete_request": "R-DEMO-A",
    "b_incomplete_then_clarified": "R-DEMO-B",
    "d_conflicting_reply": "R-DEMO-D",
    "e_chemical_shipment": "R-DEMO-E",
    "f_door_delivery": "R-DEMO-F",
}
"""One request id per scenario directory. Demos do not invent identity at
random (``DemoSettings.deterministic``) — these are fixed, so a demo run is
reproducible and its audit trail is diffable across runs."""


def load_scenario(name: str, fixtures_root: Path) -> EmailFixtureScenario:
    """Load one named scenario by its directory name under ``fixtures_root``."""
    if name not in _SCENARIO_REQUEST_IDS:
        known = sorted(_SCENARIO_REQUEST_IDS)
        raise ValueError(f"unknown fixture scenario {name!r}; known: {known}")

    messages = load_fixture_emails(fixtures_root / name)
    return EmailFixtureScenario(
        name=name, request_id=_SCENARIO_REQUEST_IDS[name], messages=messages
    )


def load_all_scenarios(fixtures_root: Path) -> tuple[EmailFixtureScenario, ...]:
    """Load every known scenario, in a fixed (alphabetical) order."""
    return tuple(load_scenario(name, fixtures_root) for name in sorted(_SCENARIO_REQUEST_IDS))
