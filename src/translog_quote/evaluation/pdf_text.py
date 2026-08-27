"""PDF to text. Deterministic, offline, and not a model's job.

Client cases arrive as Gmail "print to PDF" thread exports. Those carry page
furniture — a repeated header line and a footer with the mailbox URL — that
says nothing about the shipment. Removing it here, with rules, means the model
never has to ignore it, and never has a chance to mistake it for content.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pypdf

if TYPE_CHECKING:
    from pathlib import Path

#: "25/07/26, 9:08 PMTranslog Express Pvt Ltd Mail - <subject>"
_PAGE_HEADER = re.compile(r"^\d{1,2}/\d{1,2}/\d{2},\s*\d{1,2}:\d{2}\s*[AP]M.*Mail\s*-")

#: "Page 3 of 10https://mail.google.com/mail/u/..."
_PAGE_FOOTER = re.compile(r"^Page\s+\d+\s+of\s+\d+", re.IGNORECASE)

_BARE_MAIL_URL = re.compile(r"^https?://mail\.google\.com/\S*$", re.IGNORECASE)


def read_pdf_text(path: Path) -> str:
    """Extract a PDF's text, page order preserved."""
    reader = pypdf.PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def strip_page_furniture(text: str) -> str:
    """Drop repeated print-export headers, footers and bare mailbox URLs.

    Nothing content-bearing matches these patterns: each is anchored to the
    start of a line and to a shape the export generates, not one a person
    writes.
    """
    kept = [
        line
        for line in text.splitlines()
        if not (
            _PAGE_HEADER.match(line.strip())
            or _PAGE_FOOTER.match(line.strip())
            or _BARE_MAIL_URL.match(line.strip())
        )
    ]
    return "\n".join(kept)


def normalise_whitespace(text: str) -> str:
    """Collapse the ragged blank runs a PDF export leaves behind.

    Trailing spaces and three-or-more blank lines carry no meaning and cost
    tokens on every request.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    out: list[str] = []
    blanks = 0
    for line in lines:
        if line.strip():
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks <= 1:
                out.append("")
    return "\n".join(out).strip()


def pdf_to_clean_text(path: Path) -> str:
    """The full deterministic ingestion step for one client case."""
    return normalise_whitespace(strip_page_furniture(read_pdf_text(path)))
