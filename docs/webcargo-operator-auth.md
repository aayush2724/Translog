# WebCargo browser worker — operator authentication (manual, one-time)

The rate-search worker drives a **persistent** headless Chromium profile that
holds the authenticated WebCargo session. That session is established **once,
by a human**, and reused across every job and worker restart. Nothing in the
codebase automates, fills, or bypasses login, MFA, or CAPTCHA — this document
is the only way the profile becomes authenticated.

## Why it is manual

The worker never logs in. It either finds a live authenticated session in the
profile or, on a `WebCargoSessionLost`, stops for an operator to re-authenticate
(requeuing any in-flight job first — see "When the session expires"). Sign-in —
including any MFA or CAPTCHA — is a person's action, performed here.

## Prerequisites (one time per machine)

```bash
# From the repo root, in the project venv:
pip install -e '.[worker]'          # playwright, redis, rq
python -m playwright install chromium
# A real (headed) browser is required — WebCargo refuses headless sessions.
# On a server with no physical display, install a virtual display:
sudo dnf install xorg-x11-server-Xvfb   # Fedora/RHEL
# (Debian/Ubuntu: sudo apt-get install xvfb)
```

**Why headed + Xvfb:** WebCargo redirects any `HeadlessChrome` client to
login, so the worker runs a genuine headed Chromium. On a headless server that
browser renders to a virtual display (Xvfb) via `xvfb-run`. This is a real
browser on a virtual screen — no user-agent or fingerprint disguising.

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

The worker runs a **headed** browser. On a machine with a physical display it
runs directly; on a headless server, run it under a virtual display:

```bash
# Server (no physical display) — real browser on a virtual screen:
xvfb-run -a --server-args="-screen 0 1280x720x24" \
  python -m translog_quote.interface.worker

# Machine with a display:
python -m translog_quote.interface.worker
```

One worker per queue (enforced by a Redis lock — a second worker refuses).
It launches the persistent profile **headless**, reuses that one session for
every job, and creates a fresh page per job.

## When the session expires

The worker never crash-loops on a lost session. What happens depends on when
the loss is discovered:

- **At startup** the auth probe is retried a few times first (a freshly
  launched profile can miss the first navigation-timeout window — a cold-start
  race, not a real loss). If it still finds no session, the worker records
  `needs_login` in Redis (`translog:rate-search:worker-status`, TTL), releases
  the browser and the single-worker lock, and **exits cleanly with code 78
  (`EX_CONFIG`)**. The systemd unit sets `RestartPreventExitStatus=78`, so it is
  **left stopped, not restarted** — no thrash.
- **Mid-job** the failing job is **requeued at the front of the queue** (it does
  *not* end permanently `FAILED`), and the worker then exits the same way
  (status key + code 78). Because queued jobs carry no TTL, the search simply
  waits.

Either way the dashboard shows the waiting request as *"Rate-search worker
needs sign-in — searches are queued until an operator re-authenticates"* rather
than an indefinite pending state.

**Recovery — the worker will NOT restart itself after code 78:**

```bash
# 1. Sign in again (opens the headed browser; a person completes WebCargo login):
systemctl --user stop translog-webcargo-worker            # if it is somehow still up
xvfb-run -a python -m translog_quote.interface.worker --login --env-file .env.worker
#    …complete sign-in until the Search & Book form is visible, then Ctrl-C.
# 2. Start the service again — required, because RestartPreventExitStatus=78
#    means systemd did not restart it for you:
systemctl --user start translog-webcargo-worker
```

A worker that starts with a valid session deletes the `needs_login` key, so the
dashboard indicator returns to *online* on its next poll. (After editing the
unit, `systemctl --user daemon-reload` once so `RestartPreventExitStatus` takes
effect.)

## Deployment note — persistence is required

`USER_DATA_DIR` must live on **persistent storage**. On an ephemeral
filesystem (a rebuilt container, a redeploy without a mounted disk) the
profile — and therefore the authenticated session — is wiped, and an operator
must re-authenticate. Mount a disk for it, exactly as the demo already does
for its state directory in `render.yaml`.

## Deployment topology — Render dashboard → Upstash Redis → Fedora worker

The production POC is a **hybrid**: a lightweight cloud dashboard that never
touches WebCargo, joined over a shared managed Redis to the single Fedora
browser worker that does.

```
Render web service ("translog-demo")            Fedora box
  interface.web --live                            systemd --user:
  MODE=browser  ──enqueue RateSearchJobRequest──▶   translog-webcargo-worker
        ▲                                            xvfb-run → headed Chromium
        └────────── poll fetch_job_status ───────    → WebCargo (authenticated)
                            │
                Upstash Redis (TLS, DB 0, queue "rate-search")
                   also shared with DeskCart — namespaced keys only
```

**Who holds what.**
- **Render** runs only the dashboard. In `browser` mode it *enqueues* a job and
  *polls* the result — it builds no browser, runs no Playwright/Chromium/Xvfb,
  and holds **no WebCargo credentials**. Its only queue setting is
  `TRANSLOG_QUEUE__REDIS_URL` (a dashboard secret, `sync: false`). The build
  installs the `[api]` extra so `redis`/`rq` are present to enqueue and poll.
- **Fedora** runs the one browser worker (this document) — the only process with
  the authenticated WebCargo session. It is the sole queue consumer; the Redis
  lock `translog:rate-search:browser-worker` guarantees no second worker.
- **Upstash Redis** is the join: both sides use the **same** endpoint, **DB 0**,
  and the default `rate-search` queue name. Idempotency keys are deterministic,
  so a job Render enqueues is exactly the job Fedora runs.

**Both sides must agree** or jobs never meet: identical `TRANSLOG_QUEUE__REDIS_URL`
(same host, `/0`), and neither side overriding the default queue name.

**If the Fedora worker is offline:** the dashboard still enqueues; the job sits
`QUEUED` and the request shows "Searching WebCargo…" until the worker returns and
processes it. Browser mode never falls back to simulated data — a genuine failure
is reported, not papered over. (A worker SIGKILLed *mid-job* leaves that job
`processing` until its `job_timeout` elapses — a documented POC limitation; no
automatic retry or resume.)

**Staged rollout / rollback.** Bring the Fedora worker up first; deploy the
`[api]` build and set `TRANSLOG_QUEUE__REDIS_URL` while still in `demo` mode;
then flip `TRANSLOG_WEBCARGO__MODE` demo→browser **last**. Rollback is the
reverse: set `MODE=demo` and redeploy — the dashboard resumes synchronous,
disclosed, simulated rates with no worker involved.

**Render plan.** Requires the Starter plan and the persistent disk already in
`render.yaml`. The Free tier has no disks and spins down when idle, which would
drop durable approval state and stop the dashboard polling for completed jobs.

## Deploying the goods-type update (required deploy order)

The WebCargo Goods Type is now a controlled field decided **before enqueue**
(the reviewed General Cargo rule or an operator pick), never the client's
free-text commodity. `goods_type` became a **required** field on the queued job,
so old and new formats must not cross. Deploy in this order:

```
1. systemctl --user stop translog-webcargo-worker     # no OLD worker reads NEW jobs
2. merge/deploy                                        # Render redeploys the dashboard
3. git pull && pip install -e '.[api,worker]'          # on the Fedora box
4. systemctl --user start translog-webcargo-worker      # the NEW worker consumes NEW jobs
```

Stopping the worker first closes the only failure window (a *new-format* job read
by an *old* worker rejects on the unknown field). **An old-format job still
queued at deploy** (no `goods_type`) is read by the new worker and fails with a
plain message — *"This rate search was queued before the goods-type update…"* —
not a raw validation error, and never reaches WebCargo. It has **no effect**:
after the dashboard restarts, each `VALIDATED` request **re-derives** its
goods-type decision under the new rule (General Cargo, or an operator hold) and
re-enqueues under a **new idempotency key** (goods_type changes the digest), so
the orphaned old job is simply superseded and expires under its `failure_ttl`.

### Goods-type configuration (JSON lists, empty by default)

```
TRANSLOG_GOODS_TYPE__GENERAL_CARGO_LABEL='0000 - General Cargo'   # confirm against the live dropdown
TRANSLOG_GOODS_TYPE__CATALOG='["0000 - General Cargo", "1234 - Machinery"]'
TRANSLOG_GOODS_TYPE__SPECIAL_HANDLING='["battery", "lithium", "perishable"]'
```

- `SPECIAL_HANDLING` **empty ⇒ the General Cargo rule is OFF**: every request
  holds for an operator. The business enables the rule by populating it (a
  reviewed starter set: battery, lithium, airbag, perfume, fresh, frozen,
  chilled, perishable, pharma, medicine, vaccine, live, animal, gold, jewel,
  valuable, dry ice, magnet, aerosol). Matching is whole-word/phrase on the
  commodity — a false positive only routes to an operator (the safe direction).
- `CATALOG` is the operator's pick list; the `GENERAL_CARGO_LABEL` is always a
  member. An empty/unconfigured catalog shows "goods-type catalog not configured"
  on the hold rather than an empty picker.
- General Cargo is chosen automatically only when `cargo_type` normalises to a
  reviewed accepted phrase (`general cargo`, `non hazardous`, `non haz`, and the
  two combined phrasings), `is_chemical` is explicitly `False`, and no
  special-handling word is on the commodity. Anything else holds.

## Worker log notes (after the reliability round-2 deploy)

- **`lock lapsed, re-acquired` (WARNING) is expected, not a fault.** When Upstash
  resets the connection long enough for the 120s lock lease to lapse, the
  heartbeat now re-acquires the lease with the same token and logs this once,
  instead of self-stopping the worker (which is what the old behaviour did).
  Occasional occurrences after a broker blip are normal. What is NOT normal is
  the worker actually stopping (`self-stopping (...)`) — that only happens on a
  genuinely different lock holder or a lost session.
- **Startup env for the unreachable bound:**
  `TRANSLOG_WEBCARGO__STARTUP_UNREACHABLE_MAX_WAIT_SECONDS` (default 120) bounds
  how long the worker retries a boot-before-network before exiting 75 for a
  later systemd restart — it never writes `needs_login`.
- **Verify after deploy:** `python -m translog_quote.ops.healthcheck --env-file
  .env.worker` (add `--token`, with `TRANSLOG_DASHBOARD_TOKEN` in the env, for
  the dashboard checks 8-9). `journal(200)` self-stops/reconnects should trend to
  ~0 once the fix is running.
