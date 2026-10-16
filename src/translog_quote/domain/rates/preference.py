"""What the client said they care about: speed, price, or neither.

Deterministic on purpose. The preference only *recommends* which option the
dashboard preselects — the operator still chooses, and nothing is sent without
their approval — so a short, reviewable phrase list is enough, costs no model
call, and behaves identically across runs. It never decides anything on its own.

Conservative by construction:

- Only explicit wording counts ("fastest", "urgent", "cheapest", "lowest
  price"). Vague hints ("soon", "good rate") say nothing.
- A negated phrase does not count: "not urgent", "no rush", "doesn't need to be
  the fastest". It is not read as the opposite preference either — "not urgent"
  is not a request for the cheapest rate.
- Quoted earlier mail (lines starting ">" and everything below an "On … wrote:"
  or "Original Message" marker) is ignored, so a reply does not re-read our own
  or the client's earlier text as new wording.
- Speed and price wording in the same message is UNCLEAR: the operator decides.
"""

from __future__ import annotations

import re
from enum import StrEnum

from translog_quote.domain.rates.strategy import RateStrategy


class RatePreference(StrEnum):
    """The client's stated priority, as read from their own words."""

    FASTEST = "fastest"
    CHEAPEST = "cheapest"
    UNCLEAR = "unclear"
    """Both speed and price wording appeared; neither is assumed."""


_SPEED = re.compile(
    r"\b(?:fastest|quickest|earliest(?: possible| available)?(?: flight| departure)?"
    r"|as soon as possible|asap|urgent(?:ly)?|time[- ]critical|time[- ]sensitive"
    r"|express(?: service| shipment| delivery)?|shortest transit(?: time)?"
    r"|speed is (?:the |our )?(?:priority|important|key)"
    r"|(?:need|needs|needed) (?:it|this|them|the (?:cargo|shipment|goods)) quickly"
    r"|rush(?:ed)? (?:shipment|delivery|job))\b"
)

_PRICE = re.compile(
    r"\b(?:cheapest|cheaper|least expensive|lowest (?:price|rate|cost|quote|tariff|fare)"
    r"|best (?:price|rate)|most economical|economical|economy option|budget"
    r"|low(?:er)?[- ]cost|cost[- ](?:effective|efficient|sensitive)"
    r"|(?:price|cost|budget) is (?:the |our |more )?(?:priority|important|key|a concern)"
    r"|affordable)\b"
)

#: A negation in the few words just before a phrase cancels it. Looked for only
#: inside the same clause (no sentence break in between).
_NEGATION = re.compile(
    r"\b(?:not|no|don'?t|doesn'?t|isn'?t|aren'?t|never|without|nor|neither)\b"
    r"(?:\W+\w+){0,4}\W*$"
)

#: Where quoted earlier mail begins. Not "From:" — clients write "From: Delhi".
_QUOTE_START = re.compile(
    r"^\s*(?:on .{0,200}wrote:|-{2,}\s*original message\s*-{2,})", re.IGNORECASE
)


def _own_words(text: str) -> str:
    """The client's new text only: quoted lines and earlier mail removed."""
    kept: list[str] = []
    for line in text.splitlines():
        if _QUOTE_START.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).lower()


def _stated(pattern: re.Pattern[str], text: str) -> bool:
    """Whether ``pattern`` occurs at least once without a negation before it."""
    for match in pattern.finditer(text):
        before = text[max(0, match.start() - 40) : match.start()]
        # Only the current clause: a negation in an earlier clause does not
        # reach across a full stop, comma, line break or semicolon ("No rush,
        # but cheapest please" still asks for the cheapest).
        clause = re.split(r"[.,!?;\n]", before)[-1]
        if not _NEGATION.search(clause):
            return True
    return False


def detect_rate_preference(subject: str, body: str) -> RatePreference | None:
    """The preference one message states, or ``None`` when it states none."""
    text = _own_words(f"{subject}\n{body}")
    speed = _stated(_SPEED, text)
    price = _stated(_PRICE, text)
    if speed and price:
        return RatePreference.UNCLEAR
    if speed:
        return RatePreference.FASTEST
    if price:
        return RatePreference.CHEAPEST
    return None


def recommended_strategy(preference: RatePreference | None) -> RateStrategy:
    """The option the dashboard preselects for this client.

    CHEAPEST only when the client clearly asked for price. Everything else —
    speed, mixed wording, or nothing said — keeps the existing default of
    fastest eligible (BR-1), so an unclear request behaves exactly as before.
    """
    if preference is RatePreference.CHEAPEST:
        return RateStrategy.CHEAPEST
    return RateStrategy.FASTEST
