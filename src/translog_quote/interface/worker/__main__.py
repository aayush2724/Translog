"""`python -m translog_quote.interface.worker` — run the browser worker.

`--login` is the explicit operator sign-in path, folded into worker startup:
the worker opens its ONE long-lived persistent context, a person completes
the WebCargo sign-in — MFA and all — themselves, and once the search form is
visible the SAME process (and SAME live context) begins serving jobs. The
browser is never closed and reopened, so the in-memory session cookie that
authenticates the app is never dropped. Nothing automates, fills, or bypasses
any login control, and no cookie is ever exported.

Without `--login` the worker expects an already-authenticated live session
and refuses loudly if it is missing, rather than logging in automatically.
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
            "let an operator sign in to WebCargo at startup, in the worker's own "
            "live browser context, then serve the queue in this same process "
            "(the browser stays open so the session is not lost)"
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

    from translog_quote.interface.worker.main import run_worker

    run_worker(settings, interactive_login=args.login)
    return 0


if __name__ == "__main__":
    sys.exit(main())
