# WebCargo browser worker — operator authentication (manual, one-time)

The rate-search worker drives a **persistent** headless Chromium profile that
holds the authenticated WebCargo session. That session is established **once,
by a human**, and reused across every job and worker restart. Nothing in the
codebase automates, fills, or bypasses login, MFA, or CAPTCHA — this document
is the only way the profile becomes authenticated.

## Why it is manual

The worker never logs in. On any job it either finds a live authenticated
session in the profile or fails the job with `WebCargoSessionLost` and refuses
every later job until an operator re-authenticates. Sign-in — including any
MFA or CAPTCHA — is a person's action, performed here.

## Prerequisites (one time per machine)

```bash
# From the repo root, in the project venv:
pip install -e '.[worker]'          # playwright, redis, rq
python -m playwright install chromium
```

Configuration (in `.env` or the environment) — no endpoint is written in the
repo, so the operator supplies it:

```
TRANSLOG_WEBCARGO__MODE=browser
TRANSLOG_WEBCARGO__BASE_URL=<the authorized WebCargo app URL you sign in to>
TRANSLOG_WEBCARGO__USER_DATA_DIR=.browser/webcargo-profile   # default; see deploy note
TRANSLOG_QUEUE__REDIS_URL=redis://localhost:6379/0           # your Redis
```

## The authentication step

```bash
python -m translog_quote.interface.worker --login
```

This opens a **headed** Chromium on the persistent profile directory and
navigates to `TRANSLOG_WEBCARGO__BASE_URL`. Then, in that window:

1. Sign in to WebCargo yourself — username, password, and any MFA/CAPTCHA.
2. Wait until you can see the eBooking **Search and book** screen.
3. Return to the terminal and press **Enter**.

The command closes the browser cleanly, leaving the authenticated cookies and
storage in the profile directory. Verify by starting the worker (below); it
should reach the search form without a login page.

## Running the worker after authentication

```bash
python -m translog_quote.interface.worker
```

One worker per queue (enforced by a Redis lock — a second worker refuses).
It launches the persistent profile **headless**, reuses that one session for
every job, and creates a fresh page per job.

## When the session expires

A job will fail with a clear `WebCargoSessionLost` reason and every later job
will fail fast (no repeated hammering of the login page). Recovery is exactly
the step above: stop the worker, run `--login`, sign in, restart the worker.

## Deployment note — persistence is required

`USER_DATA_DIR` must live on **persistent storage**. On an ephemeral
filesystem (a rebuilt container, a redeploy without a mounted disk) the
profile — and therefore the authenticated session — is wiped, and an operator
must re-authenticate. Mount a disk for it, exactly as the demo already does
for its state directory in `render.yaml`.
