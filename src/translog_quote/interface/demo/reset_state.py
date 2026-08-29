"""Clear the local demonstration state, and nothing else.

What this removes: the JSON files under the configured state directory that
record which messages have been processed and what has already been sent.

What it cannot touch, structurally rather than by promise:

- **Gmail.** No mailbox client is imported here, no credential is read, and no
  network call is made. The only module-level import that touches the outside
  world is `pathlib`.
- **Credentials.** `.secrets/` is a different directory and is never named.
- **Anything outside the state directory.** Only the two files the store
  writes and the audit log are removed, each by exact name. There is no
  recursive delete and no glob, so a misconfigured `state_dir` pointing at a
  real directory removes at most three files that are not there.

Clearing this forgets what has already been sent, which is the point between
rehearsals and a hazard mid-run: a forgotten QUOTATION_SENT is what allows a
second quotation to reach a client. So it refuses without an explicit flag and
prints what it is about to do either way.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, TextIO

from translog_quote import bootstrap
from translog_quote.config import load_settings
from translog_quote.interface.demo.formatting import RULE, THIN
from translog_quote.interface.web.audit_log import AUDIT_FILE
from translog_quote.interface.web.demonstration import DEMONSTRATION_FILE

if TYPE_CHECKING:
    from pathlib import Path

    from translog_quote.config import Settings

EXIT_OK = 0
EXIT_REFUSED = 2

#: Every file this command may remove, by exact name. Not a glob and not a
#: directory walk: the list of what it can delete is readable here in full.
REMOVABLE = (*bootstrap.persistent_state_files(), AUDIT_FILE, DEMONSTRATION_FILE)


def run_reset_state(
    *,
    settings: Settings | None = None,
    confirmed: bool = False,
    out: TextIO = sys.stdout,
) -> int:
    """Show what would be cleared; clear it only when explicitly confirmed."""
    settings = settings or load_settings()
    directory: Path = settings.demo.state_dir

    print(f"{RULE}\n  RESET LOCAL DEMO STATE\n{RULE}", file=out)
    print(f"  state directory : {directory}", file=out)
    print(f"  files           : {', '.join(REMOVABLE)}", file=out)
    print("\n  This clears only the local record of what has been processed", file=out)
    print("  and sent. It does not touch Gmail, any message in any mailbox,", file=out)
    print("  or the OAuth credentials under .secrets/.", file=out)

    present = [directory / name for name in REMOVABLE if (directory / name).exists()]
    if not present:
        print(f"\n{THIN}\n  Nothing to clear — the demo state is already empty.\n", file=out)
        return EXIT_OK

    print(f"\n{THIN}\n  Would remove:", file=out)
    for path in present:
        print(f"    {path}", file=out)

    if not confirmed:
        print("\n  Refusing without confirmation. Clearing this forgets what has", file=out)
        print("  already been sent, so a request that was quoted could be quoted", file=out)
        print("  again. Re-run with --yes if that is what you want:\n", file=out)
        print("    python -m translog_quote.interface.demo reset-state --yes\n", file=out)
        print(RULE, file=out)
        return EXIT_REFUSED

    for path in present:
        path.unlink()
        print(f"  removed {path}", file=out)
    print("\n  Demo state cleared. Gmail is untouched.", file=out)
    print(f"{RULE}", file=out)
    return EXIT_OK
