"""Client intent.

The boundary drawn here (AMB-10): the model reports *what the client said*; the
state machine decides *what follows*. UNCLEAR routes to manual review — there is
no default in either direction, because defaulting an ambiguous reply to
acceptance is a commercial act and defaulting it to rejection loses business.
"""

from __future__ import annotations

from enum import StrEnum


class ClientIntent(StrEnum):
    ACCEPT = "accept"
    DECLINE = "decline"
    UNCLEAR = "unclear"
