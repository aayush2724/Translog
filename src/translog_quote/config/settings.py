"""Typed settings.

Every field has a safe default, so `load_settings()` succeeds with no `.env` file
and no environment variables set. Nothing here reaches the network; the external
clients these values configure do not exist yet.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    REAL = "real"


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

    `mode` selects the adapter. The real adapter's credentials stay empty: no
    undocumented endpoint or credential is written anywhere in this codebase, and
    the real integration is blocked on AMB-1 and AMB-3 regardless.
    """

    mode: WebCargoMode = WebCargoMode.MOCK
    base_url: str | None = None
    username: SecretStr | None = None
    password: SecretStr | None = None


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
    gmail: GmailSettings = GmailSettings()
    demo: DemoSettings = DemoSettings()


def load_settings() -> Settings:
    """Load configuration. Safe to call with nothing configured."""
    return Settings()
