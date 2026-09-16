"""Typed settings.

Every field has a safe default, so `load_settings()` succeeds with no `.env` file
and no environment variables set. Nothing here reaches the network; the external
clients these values configure do not exist yet.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ENV_FILE = ".env"
"""The base configuration layer. Always read when it exists."""

ENV_FILE_VAR = "TRANSLOG_ENV_FILE"
"""Names a second env file, layered *on top of* `.env`.

The reason it layers rather than replaces: an account switch changes which
mailbox is read and which OAuth token files are used, and nothing else. The
OpenRouter key, the WebCargo mode and the demo paths are the same either way,
so an account file that had to restate them would mean the same secret written
in two places — the failure mode this indirection exists to avoid.
"""


class Environment(StrEnum):
    DEMO = "demo"
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class WebCargoMode(StrEnum):
    """Which adapter satisfies RateSearchPort.

    Read exactly once, in `bootstrap`. No module below the composition root may
    branch on this value — that is what keeps "swap the adapter" from degrading
    into a flag check in the middle of the pipeline.
    """

    MOCK = "mock"
    """Fixture rates, identical for every query. What the tests run on."""

    DEMO = "demo"
    """Simulated WebCargo-shaped rates, priced from the shipment being quoted.
    What the client-facing demo runs on. Still invented, still disclosed."""

    BROWSER = "browser"
    """The real provider: a persistent, authenticated WebCargo browser session
    driven by the single browser worker. Only the worker process may construct
    it — any other process asked for this mode refuses and points at the
    asynchronous rate-search API instead."""


class OpenRouterSettings(BaseModel):
    """Extraction adapter configuration."""

    api_key: SecretStr | None = None
    """No default, ever. Absent means the live adapter refuses to build."""

    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "qwen/qwen3.7-flash"
    """AMB-2, resolved in Phase 5.

    Qwen 3.7 Flash. The slug was confirmed against OpenRouter's live model list
    before being written here rather than inferred from the product name — the
    catalogue also carries `qwen3.7-plus`, `qwen3.7-max` and `qwen3.6-flash`,
    any of which a guess could plausibly have landed on.

    The model advertises `response_format` but not `structured_outputs`, so the
    adapter asks for JSON mode and enforces the schema itself.
    """

    timeout_seconds: int = Field(default=60, gt=0)
    max_retries: int = Field(default=2, ge=0)
    retry_backoff_seconds: float = Field(default=2.0, ge=0)
    """Base delay before the first retry, doubling thereafter with jitter.

    Only used when the provider does not send a `Retry-After` of its own. The
    upstream this project talks to answers a 429 with "retry shortly" and no
    header, so some backoff of our own is the difference between waiting and
    hammering.
    """


class WebCargoSettings(BaseModel):
    """Rate provider configuration.

    `mode` selects the adapter. The credential fields exist for the browser
    worker's one-time interactive login; no credential is ever written in this
    codebase, and the simulated modes need none.
    """

    mode: WebCargoMode = WebCargoMode.MOCK
    base_url: str | None = None
    username: SecretStr | None = None
    password: SecretStr | None = None

    # --- browser worker (mode=browser) --------------------------------------

    user_data_dir: Path = Path(".browser/webcargo-profile")
    """Where the persistent Chromium profile lives — cookies, local storage,
    and therefore the authenticated WebCargo session that must survive across
    jobs and across worker restarts.

    **Deployment note:** this only persists if the directory itself does. An
    ephemeral filesystem (a rebuilt container, a redeployed instance without a
    disk) wipes the profile and the operator must re-authenticate. Point this
    at mounted persistent storage in any real deployment, exactly as
    `render.yaml` already does for the demo's state directory."""

    headless: bool = False
    """Run a real, headed Chromium — never headless.

    WebCargo refuses to honour an authenticated session from a headless
    browser: any client advertising the ``HeadlessChrome`` user-agent is
    redirected to login, and every supported headless mode (Chromium old and
    new headless, and the real-Chrome channel) emits that token. This was
    established by read-only investigation; the only fixes that would make
    headless authenticate are user-agent/fingerprint spoofing, which this
    project does not do.

    So the worker runs a genuine headed browser. On a server with no physical
    display that means a **virtual display (Xvfb)** — launch the worker under
    ``xvfb-run`` (see ``docs/webcargo-operator-auth.md``). This is not
    disguising the browser: it is a real Chromium rendering to a virtual
    screen. The operator re-authentication command is headed for the same
    reason. Nothing here spoofs identity, and no headless mode is offered,
    because none authenticates against this provider."""

    navigation_timeout_seconds: int = Field(default=30, gt=0)
    """Ceiling for one page navigation inside the WebCargo UI."""

    startup_unreachable_max_wait_seconds: float = Field(default=120.0, gt=0)
    """How long the worker keeps retrying (with backoff) when WebCargo is
    UNREACHABLE at startup — DNS/connection/timeout/navigation failure, e.g.
    booting before the network is up — before giving up and exiting
    ``EXIT_UNREACHABLE`` (75) so systemd restarts it later. This is NOT the
    needs-login path and never writes ``needs_login``. Env:
    ``TRANSLOG_WEBCARGO__STARTUP_UNREACHABLE_MAX_WAIT_SECONDS``."""

    search_timeout_seconds: int = Field(default=180, gt=0)
    """Ceiling for one complete rate search — form fill, results, detail
    reads, pagination. A job that cannot finish inside this is failed with the
    reason rather than left holding the worker."""


class QueueSettings(BaseModel):
    """The Redis/RQ job queue joining the API service to the browser worker.

    One queue, one worker, one job at a time. The single-worker rule is an
    architectural correctness decision: the browser session is shared state,
    WebCargo interaction is stateful, and serial execution keeps runs
    deterministic, keeps provider load controlled, and keeps failures easy to
    reason about.
    """

    redis_url: str = "redis://localhost:6379/0"

    rate_search_queue: str = "rate-search"
    """The queue name both sides agree on. API-side pagination of jobs and
    WebCargo's own result-table pagination are unrelated concepts."""

    job_timeout_seconds: int = Field(default=600, gt=0)
    """RQ kills a job that exceeds this — a hung browser search must not hold
    the single worker forever."""

    result_ttl_seconds: int = Field(default=24 * 3600, gt=0)
    """How long a finished job's result stays fetchable. Also the idempotency
    window: an identical request inside it returns the same job."""

    failure_ttl_seconds: int = Field(default=7 * 24 * 3600, gt=0)
    """Failed jobs are kept longer than results: a failure is evidence."""

    worker_lock_key: str = "translog:rate-search:browser-worker"
    """The startup lock guaranteeing one browser worker per queue. A second
    worker started by accident refuses loudly instead of silently running
    concurrent WebCargo automation."""

    worker_lock_ttl_seconds: int = Field(default=120, gt=0)
    """Lock lease; the running worker refreshes it, and a crashed worker's
    lock expires on its own so a restart is never wedged."""


class GmailSettings(BaseModel):
    """Gmail test-mailbox configuration (Phase 10.3 — receive-only).

    Everything here is **temporary test plumbing** for one personal test
    mailbox, not the production mailbox integration. No address is written in
    source; no password is ever asked for anywhere — authentication is OAuth
    2.0, and the OAuth files live at git-ignored paths outside version control.
    """

    test_address: str | None = None
    """The test mailbox to read. TRANSLOG_GMAIL__TEST_ADDRESS. No default,
    ever: absent means the Gmail source refuses to build, and the adapter
    additionally refuses to run against any mailbox other than this one."""

    client_secret_path: Path = Path(".secrets/gmail_client_secret.json")
    """The OAuth client file downloaded from Google Cloud console (Desktop
    app). Read only by the one-time `gmail-auth` consent command."""

    token_path: Path = Path(".secrets/gmail_token.json")
    """Where the consent command stores the authorized-user token (refresh
    token + client id/secret). Created chmod 0600; `.secrets/` is git-ignored."""

    query: str = "in:inbox"
    """Gmail search scope for the test fetch. Deliberately narrow: the inbox
    only — never sent mail, never the whole mailbox."""

    max_results: int = Field(default=1, ge=1, le=10)
    """How many messages one fetch may retrieve. The Phase 10.3 test needs
    exactly one."""

    fetch_cap: int = Field(default=25, ge=1, le=500)
    """The per-poll ceiling on *client* messages fully fetched in operations
    mode's date-bounded fetch.

    Operations mode reads ``in:inbox after:<watermark - overlap>`` and pages the
    id list to the end (cheap, ids only), oldest-first. This bounds how many of
    those it then fully retrieves in one poll: a mailbox that accumulated more
    than this during a long downtime is drained across successive polls rather
    than in one burst, and internal/approver mail never spends the budget. The
    watermark only advances to the newest message actually handled, so the
    remainder is picked up next poll."""

    timeout_seconds: int = Field(default=30, gt=0)
    max_retries: int = Field(default=2, ge=0)
    retry_backoff_seconds: float = Field(default=2.0, ge=0)

    # --- outbound (Phase 11 — sending) -------------------------------------
    #
    # Deliberately a second set of fields behind a second token file. The
    # inbound credential holds `gmail.readonly` and *cannot* send whatever the
    # code does; the outbound credential holds `gmail.send` and cannot read.
    # Keeping them apart is what makes "inbound processing is separate from the
    # outbound sink" a property of the credentials rather than of the code.

    send_enabled: bool = False
    """Master switch for outbound Gmail. Off unless explicitly turned on.

    Off is the safe default: with it off `build_gmail_email_sink` refuses, and
    every demo falls back to the outbox sink that delivers nothing. Nobody
    mails a real client because a token file happened to be present.
    """

    send_token_path: Path = Path(".secrets/gmail_send_token.json")
    """Where the `gmail-auth-send` consent command stores the send-scoped
    authorized-user token. A *different* file from `token_path`, so the
    read-only credential and the send credential are never the same object."""

    sender_address: str | None = None
    """The mailbox outbound messages are sent from — Translog's own account.

    Written into the `From` header, which Gmail itself validates against the
    authenticated user: a mismatch is rejected by the provider rather than
    silently sent from somewhere else. Falls back to `test_address` in the
    composition root when unset, because in this demo Translog reads and sends
    from one mailbox."""

    approver_address: str | None = None
    """The internal mailbox the quotation review packet is sent to.

    Never a client address. It receives the full review — excluded carriers,
    runner-up rates, exclusion reasons — which is internal commercial detail
    and must not reach a client."""


class DemoSettings(BaseModel):
    fixtures_dir: Path = Path("fixtures/scenarios")
    """Full end-to-end demo bundles (Phase 8): emails, cached model responses
    and rate sets, grouped per S1-S4 business scenario."""

    email_fixtures_dir: Path = Path("fixtures/emails")
    """Raw client email fixtures (Phase 3), grouped per named input scenario.
    Narrower than `fixtures_dir`: just the email/thread layer, independent of
    which S1-S4 business scenario eventually consumes it."""

    outbox_dir: Path = Path("outbox")

    state_dir: Path = Path("runs/state")
    """Where the durable demo store keeps what has already happened.

    Git-ignored (`runs/`). The real-Gmail demo spans several CLI invocations —
    a clarification goes out today, the client replies tomorrow — so what was
    already sent has to outlive the process that sent it. Deleting this
    directory starts a fresh demonstration and forgets what was sent, which is
    exactly what you want between rehearsals and never what you want mid-run.
    """

    deterministic: bool = True
    """Fixed clock, cached model responses, fixture-assigned request ids.

    Demos that drift are not demonstrations.
    """

    poll_interval_seconds: float = Field(default=10.0, gt=0)
    """How often the live server reads the mailbox on its own.

    The live demonstration has no "check mail" control: the server polls in the
    background and the browser watches the state that produces. This is the gap
    between one poll finishing and the next one starting.

    A poll costs one Gmail list plus one get per listed message, and — because
    a message already routed is skipped *before* extraction — no model call at
    all unless something new arrived. Ten seconds is therefore cheap, and about
    as long as a room will watch a dashboard before believing it is broken.
    """

    startup_mode: Literal["demonstration", "operations"] = "demonstration"
    """What a process restart means.

    ``demonstration`` (the default, for local rehearsals): every boot starts a
    fresh demonstration — cutoff ``now``, empty view — exactly as before.

    ``operations`` (the Render deployment): a restart does **not** start a new
    demonstration. Non-terminal requests are restored from the durable store and
    the mail cutoff is the last successful poll, so a deploy neither empties the
    dashboard nor drops mail that arrived during it. Starting a fresh
    demonstration stays an explicit operator action, never a side effect of a
    restart."""

    operations_since: datetime | None = None
    """The first mail cutoff for operations mode, used only until the first
    successful poll persists a real watermark.

    Required in operations mode when no watermark exists yet (a fresh disk, or
    the first deploy after this change): the server refuses to start without it
    rather than silently defaulting to ``now`` and skipping every message that
    arrived earlier. Set it (ISO 8601, e.g. ``2026-09-16T00:00:00+00:00``) to
    the instant from which mail should be considered. Ignored once a watermark
    has been written."""

    fetch_overlap_minutes: float = Field(default=30.0, ge=0)
    """How far *before* the watermark the operations-mode fetch reaches.

    The date-bounded query is ``after:<watermark - overlap>``. The overlap is
    safe — re-listed messages already handled are dropped by the durable
    ``already_processed`` check — and guards the boundary against clock skew and
    a message that landed in the same second the watermark was taken."""

    stale_operator_hours: float = Field(default=24.0, gt=0)
    """Healthcheck threshold (operations mode): a non-terminal request in an
    operator-owned state — a goods-type/clarification hold, an approval, or a
    VALIDATED awaiting its search — older than this is flagged as stuck."""

    stale_clarification_hours: float = Field(default=72.0, gt=0)
    """Healthcheck threshold (operations mode) for CLARIFICATION_SENT, which
    legitimately waits on a *client* reply and so is given longer than an
    operator-owned state before it is flagged."""

    @field_validator("operations_since")
    @classmethod
    def _assume_utc(cls, value: datetime | None) -> datetime | None:
        """A cutoff written without a timezone is read as UTC, so it can be
        compared against the timezone-aware receipt times Gmail returns."""
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class GoodsTypeSettings(BaseModel):
    """How a validated shipment's cargo becomes a WebCargo Goods Type.

    WebCargo's Goods Type is a controlled dropdown; the client's free-text
    commodity is never typed into it. Instead the web process decides an exact
    WebCargo label before enqueue — the reviewed "General Cargo" label when the
    cargo is unambiguously general/non-hazardous, otherwise an operator picks
    one from the reviewed ``catalog``.

    Both lists are JSON arrays of *exact WebCargo labels*, empty by default so
    the business enables them deliberately:

        TRANSLOG_GOODS_TYPE__CATALOG='["0000 - General Cargo", "1234 - Machinery"]'
        TRANSLOG_GOODS_TYPE__SPECIAL_HANDLING='["battery", "lithium", "perishable"]'

    ``special_handling`` is matched by normalised whole-word/phrase hits against
    the commodity (a false positive only routes to an operator — the safe
    direction). While it is EMPTY the General Cargo rule is OFF: every shipment
    goes to an operator decision, never a silent default.
    """

    model_config = ConfigDict(frozen=True)

    general_cargo_label: str = "0000 - General Cargo"
    """The exact WebCargo Goods Type label selected for general cargo. A config
    value, not a hard-coded string, so the business can correct it against the
    live dropdown without a code change; the adapter fails loudly if WebCargo
    does not offer it. Confirm against the real Goods Type list before relying
    on it."""

    catalog: tuple[str, ...] = ()
    """The exact WebCargo labels an operator may pick on a goods-type hold. The
    configured ``general_cargo_label`` is always treated as a member. Empty (or
    only the general-cargo label) means the hold reports 'goods-type catalog not
    configured' rather than an empty picker."""

    special_handling: tuple[str, ...] = ()
    """Normalised words/phrases that force an operator decision (e.g. battery,
    perishable). Empty disables the automatic General Cargo rule entirely. A
    reviewed starter set for the business to consider (NOT enabled here):
    battery, lithium, airbag, perfume, fresh, frozen, chilled, perishable,
    pharma, medicine, vaccine, live, animal, gold, jewel, valuable, dry ice,
    magnet, aerosol."""


class Settings(BaseSettings):
    """Root settings. Nested sections use a double-underscore delimiter:

    TRANSLOG_OPENROUTER__MODEL=...
    TRANSLOG_WEBCARGO__MODE=mock
    """

    model_config = SettingsConfigDict(
        env_prefix="TRANSLOG_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Environment.DEMO
    log_level: LogLevel = LogLevel.INFO

    openrouter: OpenRouterSettings = OpenRouterSettings()
    webcargo: WebCargoSettings = WebCargoSettings()
    queue: QueueSettings = QueueSettings()
    gmail: GmailSettings = GmailSettings()
    demo: DemoSettings = DemoSettings()
    goods_type: GoodsTypeSettings = GoodsTypeSettings()


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Load configuration. Safe to call with nothing configured.

    With no `env_file` and no `TRANSLOG_ENV_FILE`, this is exactly what it has
    always been: `.env` if present, defaults otherwise.

    Given one — by argument, or by `TRANSLOG_ENV_FILE` in the environment — that
    file is layered *over* `.env`, and its values win. This is how a second
    Gmail account gets a configuration path of its own: a small file naming that
    mailbox and its own OAuth token paths, with the base file left untouched.
    Switching accounts then means selecting a file, not editing one, so the
    previous account's configuration and credentials survive the switch.

    An explicitly named file that does not exist is an error rather than a
    silent fall-back to `.env`. Falling back would run the demo against the
    *other* account while the operator believed they had switched — the one
    outcome this whole mechanism exists to prevent.
    """
    override = env_file if env_file is not None else os.environ.get(ENV_FILE_VAR) or None
    if override is None:
        return Settings()

    path = Path(override)
    if not path.is_file():
        raise FileNotFoundError(
            f"No configuration file at {path}. {ENV_FILE_VAR} (or --env-file) must name "
            "an existing env file; refusing to silently fall back to "
            f"{DEFAULT_ENV_FILE}, which may point at a different account."
        )
    # Later files win, so the account file overrides the base one field by
    # field and inherits everything it does not mention.
    return Settings(_env_file=(DEFAULT_ENV_FILE, path))  # type: ignore[call-arg]
