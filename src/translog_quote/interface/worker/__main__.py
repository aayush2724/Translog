"""`python -m translog_quote.interface.worker` — run the browser worker.

`--login` is the explicit operator re-authentication path: it opens the SAME
persistent profile the worker uses, headed, and a person completes the
WebCargo sign-in — MFA and all — themselves. Nothing automates, fills, or
bypasses any login control. Afterwards the profile on disk holds the session
and the worker can be started (or restarted) headless.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m translog_quote.interface.worker",
        description="The Translog rate-search browser worker (one per queue).",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help=(
            "open the persistent WebCargo profile in a headed browser so an "
            "operator can sign in; the session is saved in the profile directory"
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="layer this env file over .env (same semantics as the other commands)",
    )
    args = parser.parse_args(argv)

    from translog_quote import bootstrap

    settings = bootstrap.load_settings(args.env_file)

    if args.login:
        bootstrap.authorize_webcargo(settings)
        print("Session saved in the persistent profile. Start the worker to use it.")
        return 0

    from translog_quote.interface.worker.main import run_worker

    run_worker(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
