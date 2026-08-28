"""One-time interactive OAuth consent for the Gmail test mailbox.

This is setup tooling, not runtime code. It runs Google's officially supported
installed-app flow (``google-auth-oauthlib``, an optional dependency used only
here) exactly once, when a person explicitly invokes the ``gmail-auth``
command: a browser opens, the person signs in to the **test** account and
grants ``gmail.readonly``, and the resulting authorized-user token is written
to the git-ignored ``token_path`` with owner-only permissions.

No password is ever asked for or seen by this code — consent happens on
Google's own pages. Nothing here runs implicitly: importing this module does
nothing, and the runtime adapter (``gmail.py``) needs only the token file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.adapters.email.gmail import GMAIL_READONLY_SCOPE
from translog_quote.errors import PermanentFailure

if TYPE_CHECKING:
    from pathlib import Path


def run_consent_flow(*, client_secret_path: Path, token_path: Path) -> Path:
    """Run the interactive consent flow and store the token. Returns where.

    Raises ``PermanentFailure`` with exact instructions when a prerequisite is
    missing, so the demo command can print a readable next step instead of a
    stack trace.
    """
    if not client_secret_path.exists():
        raise PermanentFailure(
            f"No OAuth client file at {client_secret_path}. In Google Cloud console: "
            "create (or reuse) a project, enable the Gmail API, create an OAuth "
            "client ID of type 'Desktop app', download its JSON, and save it at "
            "that path. It is git-ignored and never committed."
        )

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise PermanentFailure(
            "google-auth-oauthlib is not installed. It is only needed for this "
            "one-time step: .venv/bin/pip install google-auth-oauthlib"
        ) from exc

    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secret_path), scopes=[GMAIL_READONLY_SCOPE]
    )
    # Loopback redirect on a random localhost port — the current officially
    # supported flow for installed apps. Blocks until the person finishes
    # consenting in the browser.
    credentials = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    token_path.chmod(0o600)
    return token_path
