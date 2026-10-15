# Deploying Translog on Railway

Two Railway services from this one repository: the dashboard (`translog-web`,
today on Render) and the WebCargo browser worker (`translog-worker`, today the
Fedora systemd unit). Everything else stays where it is: Upstash Redis is the
queue and the durable store for both, Gmail OAuth and OpenRouter are reached
from the dashboard, WebCargo from the worker. Nothing in `src/` changes; the
containers run the same two entry points with the same environment variables.

```
translog-web  (Railway, Dockerfile.web, volume /var/lib/translog)
  interface.web --live, MODE=browser ──enqueue RateSearchJobRequest──▶ Upstash Redis (TLS, DB 0, queue "rate-search")
          ▲                                                                   │
          └───────────────── poll fetch_job_status ◀──────────────────────────┤
                                                                              ▼
translog-worker (Railway, Dockerfile.worker, volume /data)        RQ SimpleWorker + single-worker lock
  Xvfb :99 → headed Chromium on /data/webcargo-profile ───────▶ WebCargo (authenticated session)
```

**One browser worker, ever.** The Redis lock `translog:rate-search:browser-worker`
refuses a second worker. Stop and disable the Fedora unit before the Railway
worker's first start, and never run both.

## Files

| file | role |
|---|---|
| `deploy/railway/Dockerfile.web` | python:3.12-slim + `pip install '.[api]'`; same build as Render |
| `deploy/railway/Dockerfile.worker` | Playwright's `python:v1.62.0-noble` image (Chromium + Xvfb preinstalled) + `pip install -c constraints.txt '.[worker]'` + x11vnc/noVNC for login mode |
| `deploy/railway/constraints.txt` | pins `playwright==1.62.0` to the base image's browser build |
| `deploy/railway/web-entrypoint.sh` | materialises the secret files, stages `account-*.json`, drops to user `translog`, starts the server |
| `deploy/railway/worker-entrypoint.sh` | Xvfb, the worker as a signal-forwarded child, exit-code contract (78 parks, others wait out the lock TTL), login mode |
| `deploy/railway/web.railway.json` | the web service's settings as a reviewable record (healthcheck `/health`, one replica). **Not attachable**: Railway deprecated Config-as-code and services created after 2026-08-28 cannot opt in, so these values are entered in the dashboard (see Services) |
| `deploy/railway/worker.railway.json` | the worker's settings as a record (no healthcheck, draining 200 s, 50 retries, one replica); same note |
| `deploy/railway/pack-secret-files.sh` | packs the Secret Files into one variable (`TRANSLOG_SECRET_FILES_TGZ_B64`) |
| `deploy/railway/pack-browser-profile.sh` | packs the authenticated profile for a one-time volume import |
| `.dockerignore` | keeps `.secrets/`, `.browser/`, `.env*`, `docs/ai/` and client data out of every image |
| `tests/unit/test_railway_config.py` | pins the invariants above against the settings defaults |

## Services

| | `translog-web` | `translog-worker` |
|---|---|---|
| Project | `invigorating-education` (reused; its earlier Railpack service was renamed) | same project |
| Source | GitHub `aayush2724/Translog`, branch `railway-deployment`, auto-deploy **off** | same repo and branch, auto-deploy off |
| Build (Settings → Build → Builder) | **Dockerfile**, path `deploy/railway/Dockerfile.web` | **Dockerfile**, path `deploy/railway/Dockerfile.worker` |
| Deploy settings (Settings → Deploy) | Healthcheck path `/health`, timeout 300; restart On Failure, 10 retries | no healthcheck; restart On Failure, **50** retries |
| Draining / overlap | variable `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=30` | variables `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=200`, `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS=0` |
| Effective start command (from the image; `startCommand` deliberately unset) | `translog-web-entrypoint python -m translog_quote.interface.web --live` | `translog-worker-entrypoint` → `Xvfb :99` + `python -m translog_quote.interface.worker` |
| Volume (service → Settings → Volume) | mount `/var/lib/translog` | mount `/data` |
| Public domain | yes: Generate Domain with target port **8765**, and set `PORT=8765` | none normally; during login mode a domain with target port **8080** and `PORT=8080` |
| Healthcheck | `GET /health` (liveness only, never `/health/ready`) | none (no HTTP port) |
| Replicas | 1 | 1 |
| Region | same for both; Southeast Asia (Singapore) is the nearest to Upstash (AWS Mumbai) and WebCargo's Indian endpoint | same |

**Config-as-code is gone for new services.** Railway's settings page now says
"Config as Code is deprecated. Existing config files keep working until
2026-12-01. Starting 2026-08-28, services that have never used Config as Code
cannot opt in." The replacement, Infrastructure as Code in
`.railway/railway.ts`, is applied only through the CLI (`railway config
plan` / `apply`). Until that is adopted, every setting above lives in the
dashboard; the two JSON files stay in the repo as the reviewed record and are
pinned by the guard test.

**Staged changes.** The dashboard stages every edit and applies them all with
one **Deploy** click; the only other option is Discard. Note that the staged
bar sits exactly where the canvas **Add** button is when the bar is absent, so
clicking "Add" by position while changes are staged hits **Deploy** instead
— open the Add menu from the top-right button (visible only when the panel is
closed) or right-click the canvas.

Why the web service has a volume: Railway refuses overlapping deployments of a
service with a volume, which is the single-instance guarantee the Render disk
gives today (one process holds the poll loop and the approval gate). It also
hosts the `accounts-config` staging dir and `TRANSLOG_DEMO__STATE_DIR`, exactly
like `render.yaml`. With `TRANSLOG_DEMO__DURABLE_BACKEND=redis` the request
store, audit log and watermark are in Upstash, so the volume holds almost
nothing and costs almost nothing.

## Variables (values omitted)

Set them per service. Copy every value from the Render service's Environment
tab and the Fedora box's `.env.worker`; nothing is re-minted or rotated.

**`translog-web`** — the Render variables, one for one, plus the two marked NEW.
Everything except the four credentials was entered on 2026-10-15; the four
credentials are entered by the operator:

| variable | note |
|---|---|
| `HOST` | `0.0.0.0` (already set in the image; harmless to repeat) |
| `TRANSLOG_ALLOWED_HOSTS` | the service's public domain, e.g. `translog-web-production.up.railway.app`; set after generating the domain, then redeploy |
| `TRANSLOG_DASHBOARD_TOKEN` | the dashboard access key — **operator enters** |
| `TRANSLOG_OPENROUTER__API_KEY` | **operator enters** |
| `TRANSLOG_OPENROUTER__MODEL` | only if Render sets it |
| `TRANSLOG_GMAIL__TEST_ADDRESS`, `TRANSLOG_GMAIL__SENDER_ADDRESS`, `TRANSLOG_GMAIL__APPROVER_ADDRESS` | as on Render |
| `TRANSLOG_GMAIL__SEND_ENABLED` | `true` |
| `TRANSLOG_GMAIL__TOKEN_PATH`, `TRANSLOG_GMAIL__SEND_TOKEN_PATH` | `/etc/secrets/gmail_token.json`, `/etc/secrets/gmail_send_token.json` (inert in multi-account mode; kept for parity) |
| `TRANSLOG_GMAIL__QUERY` | `in:inbox -subject:"[TRANSLOG INTERNAL]"` |
| `TRANSLOG_GMAIL__MAX_RESULTS` | `5` |
| `TRANSLOG_GMAIL__ACCOUNTS_DIR` | `/var/lib/translog/accounts-config` |
| `TRANSLOG_QUEUE__REDIS_URL` | the Upstash `rediss://…/0` URL, identical on both services — **operator enters** |
| `TRANSLOG_WEBCARGO__MODE` | `browser` |
| `TRANSLOG_GOODS_TYPE__GENERAL_CARGO_LABEL`, `TRANSLOG_GOODS_TYPE__CATALOG`, `TRANSLOG_GOODS_TYPE__SPECIAL_HANDLING` | as in `render.yaml` |
| `TRANSLOG_DEMO__STATE_DIR` | `/var/lib/translog/state` |
| `TRANSLOG_DEMO__STARTUP_MODE` | `operations` |
| `TRANSLOG_DEMO__OPERATIONS_SINCE` | as on Render (ignored once a watermark exists) |
| `TRANSLOG_DEMO__DURABLE_BACKEND` | `redis` — as on Render (set in its dashboard, not in `render.yaml`) |
| `TRANSLOG_SECRET_FILES_TGZ_B64` | NEW — the packed Secret Files, see below — **operator enters, and only at cut-over** (without it the entrypoint stops before polling) |
| `TRANSLOG_LOG_LEVEL` | optional |
| `PORT` | `8765` — set it explicitly and use the same number as the domain's target port; Railway's proxy and healthcheck both go to `PORT` and its injection is not guaranteed |

**`translog-worker`** — the Fedora `.env.worker`, one for one:

| variable | note |
|---|---|
| `TRANSLOG_QUEUE__REDIS_URL` | the same Upstash URL as the web service |
| `TRANSLOG_WEBCARGO__BASE_URL` | the authorised WebCargo app URL from `.env.worker` |
| `TRANSLOG_WEBCARGO__MODE` | `browser` (already in the image) |
| `TRANSLOG_WEBCARGO__USER_DATA_DIR` | `/data/webcargo-profile` (already in the image) |
| `TRANSLOG_LOG_LEVEL` | optional |
| `TRANSLOG_WEBCARGO__STARTUP_UNREACHABLE_MAX_WAIT_SECONDS` | optional, default 120 |
| `TRANSLOG_WORKER_LOGIN` | login mode only: `1` while an operator signs in, then REMOVE |
| `TRANSLOG_VNC_PASSWORD` | login mode only: the noVNC password, then REMOVE |
| `PORT` | login mode only: `8080`, the temporary domain's target port |

The worker never needs Gmail, OpenRouter or goods-type variables; the job
carries the goods-type label.

### Secret files (web)

Render mounts Secret Files at `/etc/secrets`. Railway has no equivalent, so the
same files travel in one variable and the web entrypoint writes them to
`/etc/secrets` (mode 0600, owner `translog`) at every start. The committed
account templates already use `/etc/secrets/<slot>_read.json` paths, so
`.secrets/render-accounts/*.json` work unchanged.

```bash
deploy/railway/pack-secret-files.sh            # .secrets/render-accounts + .secrets/accounts
# -> .secrets/railway/secret-files.b64 (0600). Never printed. Then either:
railway variable set "TRANSLOG_SECRET_FILES_TGZ_B64=$(cat .secrets/railway/secret-files.b64)"
# or paste the file's content into the variable in the dashboard (Raw Editor).
```

Mark `TRANSLOG_SECRET_FILES_TGZ_B64`, `TRANSLOG_QUEUE__REDIS_URL`,
`TRANSLOG_DASHBOARD_TOKEN`, `TRANSLOG_OPENROUTER__API_KEY` and (in login mode)
`TRANSLOG_VNC_PASSWORD` as **sealed** variables if your Railway plan offers
"Seal" on a variable: a sealed value can be used by the service but never
read back in the dashboard or the CLI, which is the right shape for a
credential blob. Keep the pack script's output file as your own copy.

To re-mint one token (an `invalid_grant` revocation): run the consent for that
slot (`deploy/onboard-account.sh <slot> <mailbox> --reauth`), re-run the pack
script, update the variable, redeploy. Nothing else changes.

## Deployment steps

In this order. Nothing touches Render or the Fedora worker until step 4, and
each step says how to undo it.

0. **Get the files to GitHub without touching Render.** Render auto-deploys
   `main`, so the Railway work lives on the `railway-deployment` branch
   (pushed 2026-10-15 as 940ab36). Both Railway services track that branch;
   merge to `main` after the cut-over and switch the services to `main` then.
1. **Railway account and project.** Hobby plan (Free caps a service at 0.5 GB
   RAM, Trial at 1 GB; headed Chromium peaks near 1 GB). Create an empty
   project and set its region (Southeast Asia is nearest to Upstash in AWS
   Mumbai). Account → Usage: set a usage alert (and a hard limit if offered)
   around $20/month. Keep "App Sleeping" off on both services.
2. **Service `translog-web`** (it builds immediately on creation; the first
   deploy fails for lack of variables, which is fine and holds no state):
   New → GitHub repo → `Translog`, branch `railway-deployment`. Settings →
   Build → Builder **Dockerfile**, path `deploy/railway/Dockerfile.web`;
   Deploy → Healthcheck path `/health`; Scale → region. Canvas → Add → Volume
   → attach, mount `/var/lib/translog`. Networking → Generate Domain, target
   port 8765. Source → Auto deploys → Disable.
   Variables → the web table above, `PORT=8765`, `TRANSLOG_ALLOWED_HOSTS` =
   the generated domain, `TRANSLOG_SECRET_FILES_TGZ_B64` from the pack script.
   Do not apply/deploy yet.
3. **Hand the mailboxes over, then deploy the web service.** Two dashboards
   polling the same mailboxes would create duplicate requests, so: in Render,
   Settings → **Suspend** `translog-demo` (mail simply waits in the inboxes;
   the watermark in Upstash means nothing is skipped). Then in Railway apply
   the staged variables / click Deploy. Verify: `GET /health` →
   `{"status":"ok"}`; `GET /health/ready` → `{"status":"ready","mode":"browser","redis":"ok"}`;
   logs show `materialised N secret file(s)` and `staged 2 account file(s)`;
   sign in; the desk shows the requests restored from Upstash; within a
   minute both watermarks advance (`translog:store:<account>:watermark`).
   **Undo:** Resume the Render service and remove the Railway deployment.
4. **Stop the Fedora worker:** `systemctl --user disable --now translog-webcargo-worker`,
   wait 130 s for its lease to lapse. From here until step 7 rate searches
   queue up (the dashboard says "Searching WebCargo…"); that is expected.
   **Undo:** `systemctl --user enable --now translog-webcargo-worker`.
5. **Service `translog-worker`.** New service from the same repo and branch.
   Settings → Build → Builder **Dockerfile**, path
   `deploy/railway/Dockerfile.worker`; Deploy → Restart policy On Failure,
   50 retries, no healthcheck; Scale → region. Canvas → Add → Volume →
   `/data`. No domain. Variables → the worker table. Source → Auto deploys →
   Disable. It deploys, finds no profile,
   exits 78 and parks. That is the expected first state.
6. **Bring the session over** — try the cheap route first:
   - *Profile import.* On the laptop: `deploy/railway/pack-browser-profile.sh`
     → `.secrets/railway/webcargo-profile.tar` (a credential: it holds the
     session cookies). Link the CLI (`railway login`, `railway link`,
     `railway service` → translog-worker) and upload to the volume root:
     `railway volume files upload .secrets/railway/webcargo-profile.tar /webcargo-profile.tar`
     (documented in Railway's volumes guide; confirm with `railway volume --help`).
     Redeploy the worker: the entrypoint imports the tarball because no
     profile exists yet. Logs must show `Worker … started with PID`,
     `*** Listening on rate-search...` and no `needs sign-in`.
   - *Login mode* (if it exits 78 again): Variables → `TRANSLOG_WORKER_LOGIN=1`,
     `TRANSLOG_VNC_PASSWORD=<strong>`, `PORT=8080`; Networking → Generate
     Domain with target port 8080; redeploy. Open
     `https://<worker-domain>/vnc.html`, enter the password, sign in to
     WebCargo in the visible Chromium (MFA/CAPTCHA included) until the Search
     & Book form shows. Then `railway ssh -s translog-worker` and run
     `echo > /home/translog/translog-login.fifo` in that shell. The log prints
     "Authenticated…" and the same process starts serving jobs. Remove the
     three variables and the domain, redeploy; the profile on `/data` now
     carries the session.
7. **Prove the session survives a redeploy** before trusting it: Redeploy the
   worker once more and confirm it comes back authenticated (no login mode).
8. **End-to-end check without a real client:** send one enquiry from the test
   client mailbox (your personal test address) to the account-b mailbox, watch
   the dashboard reach "Searching WebCargo…" then "awaiting approval", and
   **decline** it (declining sends nothing). Run
   `python -m translog_quote.ops.healthcheck --url https://<web-domain> --env-file .env.worker`
   from the laptop.
9. **Cut over for good:** merge `railway-deployment` into `main`, push, switch
   both Railway services to `main`. Leave Render suspended (not deleted) for a
   week as the rollback, then delete it.
10. **Rollback at any point:** resume Render, remove/stop the Railway worker
    deployment, start the Fedora unit. State is in Upstash, so either side
    resumes where the other stopped; the only thing that cannot be shared is
    the lock, hence "stop one worker before starting the other".

## Runbooks

**Session expired (worker exits 78, dashboard says "worker needs sign-in").**
The container parks itself (logs `PARKED` every 10 min; Railway does not
restart it). Set `TRANSLOG_WORKER_LOGIN=1` + `TRANSLOG_VNC_PASSWORD`, generate a
domain, redeploy, sign in via noVNC, deliver the Enter from a `railway ssh`
shell, remove the variables and the domain, redeploy. Queued jobs wait meanwhile.

**Graceful stops.** Railway sends SIGTERM; the entrypoint forwards it to the
Python worker, RQ finishes or requeues the current job, closes Chromium and
releases the lock within `drainingSeconds` (200). A redeploy of the worker
therefore takes the old one down cleanly before the new one starts (volume
services never overlap). If Railway ever SIGKILLs mid-job (draining expired),
the job stays `processing` until its 600 s `job_timeout` — the documented POC
limitation, unchanged.

**Session gone after a clean redeploy (2026-10-15).** WebCargo's auth cookie
is a session cookie and Chromium drops those after a clean shutdown unless the
profile says "continue where you left off"; the worker now writes that
preference before every launch (`ensure_session_cookies_restored`). The first
deploy carrying the fix needs one more login-mode sign-in, because the
session established before the fix was discarded at the previous restart.

**Chromium left a stale lock** (`SingletonLock` after a hard kill): the
entrypoint removes it on every start.

**Upstash blip / WebCargo unreachable at start.** The worker exits 75 (or 1);
the entrypoint waits 130 s so the lease lapses, exits, and Railway restarts the
service (`restartPolicyMaxRetries` 50 ≈ two hours of continuous failure).
After the 50th failure Railway leaves the service **crashed** until you click
Redeploy — unlike systemd's unlimited `Restart=always`. The dashboard's
worker indicator shows it; a Railway Slack/email alert on "crashed" is worth
enabling. `lock lapsed, re-acquired` warnings are expected after broker
blips, as on Fedora.

## Verification checklist

Local, before pushing (all pass as of the commit that added these files):
- `pytest -q`, `ruff check .`, `mypy src`, `git diff --check`.
- `docker build -f deploy/railway/Dockerfile.web -t translog-web:local .` and
  `… Dockerfile.worker -t translog-worker:local .`.
- Worker image smoke: headed Chromium launches on Xvfb inside the container as
  the unprivileged user and reports a non-headless user agent; `x11vnc`,
  `websockify` and `/usr/share/novnc/vnc.html` are present; `setpriv` works.
- Web image smoke: the entrypoint materialises a synthetic secret tarball,
  stages `account-*.json`, drops privileges, and `/health` answers.

On Railway (needs the real variables; cannot be proven locally):
- `/health` 200 and `/health/ready` reports `mode: browser`, `redis: ok`.
- Web logs: secret files materialised, accounts staged, both mailboxes polled
  (watermarks advance in Upstash: `translog:store:<account>:watermark`).
- Worker logs: RQ registered, listening on `rate-search`, no `needs sign-in`.
- The persistent profile survives a redeploy: redeploy the worker once and
  confirm it comes back authenticated without login mode.
- A declined test enquiry travels the whole path (step 8 above).
- A manual `railway redeploy` of the worker while idle releases and re-acquires
  the lock without a 130 s wait.

## Resources and cost

**Verified rates** (Railway pricing page, 2026-10-15): Hobby $5/month
including $5 of usage; RAM $10 per GB-month ($0.000231 per GB-minute); CPU
$20 per vCPU-month; volumes $0.15 per GB-month, billed on used space; egress
$0.05 per GB; Free plan 0.5 GB and Trial 1 GB RAM per service; usage is
billed on actual consumption, not allocation. Taxes on an Indian card are
**not verified**.

**Estimates** (the inputs are measurements on the Fedora worker, not Railway
meters): worker peak RSS 0.73–1.0 GB and near-idle CPU; dashboard a few
hundred MB. Railway meters the container's memory, which can read higher
than RSS when Chromium's on-disk cache sits in the page cache.

| | RAM | CPU | storage | USD / month |
|---|---|---|---|---|
| `translog-worker` | 0.8–1.3 GB × $10 | 0.03–0.08 vCPU × $20 | <1 GB × $0.15 | 9–15 |
| `translog-web` | 0.2–0.4 GB × $10 | 0.02–0.05 vCPU × $20 | ~0 | 2.5–5 |
| egress | | | <2 GB × $0.05 | ~0.1 |
| **bill** | | | | **≈ $12–20 / month** |

The Hobby fee is not added on top: Railway bills `max($5, usage)`, so the $5
is absorbed once usage exceeds it. At ₹95 per USD that is roughly
₹1,150–1,900 a month before any tax. The number to watch is the worker's
metered memory in the first week; if Railway reports well above 1 GB, trim
Chromium's cache by excluding more of the profile (or move the cache off the
volume) rather than paying for it. Today's equivalent spend is Render
Starter ($7 plus disk) — it goes away once the Render service is deleted.

## Known gaps and decisions

- Login mode exposes noVNC on a public Railway domain, protected by the VNC
  password over HTTPS, for the minutes a sign-in takes; remove the domain and
  the variables afterwards. The alternative (Railway TCP proxy to port 5900
  with a VNC client) works the same way and is equally temporary.
- `railway ssh` is how the "press Enter" reaches the `--login` prompt (the
  prompt reads a FIFO). Without the CLI, the profile-import route avoids the
  prompt entirely.
- The web server stops on SIGTERM without a warm phase (as on Render); a poll
  in flight is cut and repeated on the next start — the watermark only advances
  on success, so no mail is skipped.
- Variable size: the packed secret files for two mailboxes are a few KB; the
  sixteen-mailbox fleet is ~15 KB base64. If the dashboard ever rejects the
  value, split the inputs across two pack runs and two variables and extend
  the entrypoint loop — not needed today.
- `railway volume files upload` is taken from Railway's volumes guide; the
  CLI reference page lists only `download`. If your CLI lacks `upload`, use
  `railway volume browse /` (TUI upload) or login mode.
- `PORT`: Railway's healthcheck and proxy use the `PORT` variable, and the
  docs do not promise to inject it, so both services set it explicitly and the
  domain's target port must match.
- Restart counting: after `restartPolicyMaxRetries` consecutive failures the
  service stays crashed until a manual Redeploy; systemd's unlimited restarts
  have no exact equivalent here.
- Nothing here changes Render, Upstash, Gmail accounts, OAuth tokens or the
  Fedora unit; cut-over is the explicit steps 3–4 and 9.
