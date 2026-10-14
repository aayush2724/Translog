# Multi-account Gmail — example configuration (NOT committed as live config)

This directory is a **template** showing the shape of a multi-account setup for a
local two-account validation run. It contains **no credentials**. Copy it to a
real (git-ignored) location, edit the addresses, mint the OAuth tokens, and point
the dashboard at it.

## 1. The account files

One `*.json` file per Gmail mailbox. The file **stem is the `account_id`** unless
the file names one. Fields:

| field | required | notes |
|---|---|---|
| `account_id` | no | defaults to the file stem; a slug: letters/digits/`.-_` |
| `address` | yes | the mailbox this account reads; also the `From` for its replies |
| `read_token_path` | yes | this account's own `gmail.readonly` OAuth token (in `.secrets/`) |
| `send_token_path` | yes | this account's own `gmail.send` OAuth token (in `.secrets/`) |
| `approver_address` | no | internal review recipient for this account |
| `query` | no | inbound scope; default `in:inbox` (production uses `in:inbox -subject:"[TRANSLOG INTERNAL]"`) |
| `enabled` | no | default `true`; a disabled account is configured but not polled/sent from |
| `sender_address` | no | overrides `address` as the `From` if the send mailbox differs |
| `operations_since` | no | first mail cutoff (ISO-8601) until the account's first watermark |

Two live accounts must **not** share a `read_token_path` or `send_token_path`
(the loader rejects it).

## 2. Point the dashboard at a real accounts directory

```
# copy the template somewhere git-ignored and edit the addresses
cp -r deploy/accounts.example .secrets/accounts-config
$EDITOR .secrets/accounts-config/account-a.json .secrets/accounts-config/account-b.json

# enable multi-account mode
export TRANSLOG_GMAIL__ACCOUNTS_DIR=.secrets/accounts-config
```

With `TRANSLOG_GMAIL__ACCOUNTS_DIR` set, `build_live_session` builds a
`MultiAccountSession` over every enabled account. Unset, it stays the
single-account `LiveSession` (unchanged).

## 3. Mint the per-account OAuth tokens (existing tooling, no code change)

Each account needs its own read and send token. The consent commands write to
`TRANSLOG_GMAIL__TOKEN_PATH` / `TRANSLOG_GMAIL__SEND_TOKEN_PATH`, so override
those per account when running them, and sign in to the matching mailbox:

```
# account A — read token
TRANSLOG_GMAIL__TEST_ADDRESS=a@gmail.com \
TRANSLOG_GMAIL__TOKEN_PATH=.secrets/accounts/a_read.json \
python -m translog_quote.interface.demo gmail-auth

# account A — send token
TRANSLOG_GMAIL__TEST_ADDRESS=a@gmail.com \
TRANSLOG_GMAIL__SEND_ENABLED=true \
TRANSLOG_GMAIL__SEND_TOKEN_PATH=.secrets/accounts/a_send.json \
python -m translog_quote.interface.demo gmail-auth-send

# repeat for account B with b@gmail.com and .secrets/accounts/b_read.json / b_send.json
```

The token file paths must match `read_token_path` / `send_token_path` in the
account JSON.

## 4. The 16-mailbox fleet: slots `a`..`p`

Nothing in the runtime is written for two accounts — the count is simply "every
`*.json` in the accounts directory that is `enabled`". This directory commits
the full **sixteen-slot structure**, so onboarding a mailbox is mechanical:

| slot | file | state in the template |
|---|---|---|
| `a`, `b` | `account-a.json`, `account-b.json` | `enabled: true` — the shape of a live account |
| `c` … `p` | `account-c.json` … `account-p.json` | `enabled: false` — placeholder address, own token paths; flipped on once its tokens exist |

Every slot owns `.secrets/accounts/<slot>_read.json` and `<slot>_send.json`
locally, or `/etc/secrets/<slot>_read.json` and `<slot>_send.json` on Render,
so the loader's shared-credential refusal has nothing to trip on as slots are
enabled one by one. `tests/unit/test_accounts_example.py` keeps this directory
loadable as exactly those sixteen slots, with only `a` and `b` enabled.

### Onboarding one mailbox (one slot)

```
deploy/onboard-account.sh <slot> <mailbox@gmail.com> [approver@…]
```

The helper writes the slot's two config files (`.secrets/accounts-config/` for
a local run, `.secrets/render-accounts/` for Render), runs the two OAuth
consents from §3 into the slot's token files — sign in to **that** mailbox both
times — and verifies that the read token's authorised mailbox is the one named.
`--reauth` re-mints an existing slot's tokens (after an `invalid_grant`
revocation); `--no-auth` writes the config files only.

Then, in the Render dashboard, upload three Secret Files and redeploy:

| upload as | from |
|---|---|
| `account-<slot>.json` | `.secrets/render-accounts/account-<slot>.json` |
| `<slot>_read.json` | `.secrets/accounts/<slot>_read.json` |
| `<slot>_send.json` | `.secrets/accounts/<slot>_send.json` |

`render.yaml`'s start command copies every `/etc/secrets/account-*.json` into
`ACCOUNTS_DIR`, so a new slot needs no change to the service definition. The
full fleet is 16 × 3 = 48 Secret Files.

### Before the first consent for a new mailbox

- The Google OAuth app is in **Testing**: each mailbox must be listed under
  **Test users** on the consent screen (limit 100) or its consent is refused.
- A Testing app's tokens are revoked by account-level events on Google's side
  (password change, "remove third-party access", a security check-up) — account
  `a` lost its tokens twice in October 2026. Symptom: poll error
  `PermanentFailure` / `invalid_grant`, watermark frozen. Fix: `--reauth` that
  slot, re-upload its two token files, redeploy.
- One dashboard polls the accounts **serially** under one lock; with sixteen
  busy mailboxes the UI lags behind the slowest poll. Known, not yet addressed.

**How routing is guaranteed.** Every request id is namespaced `"<account_id>:…"`,
so the mailbox a request arrived on is carried in its id, not inferred. A reply,
clarification or quotation is sent through *that* account's send credential —
never a default or round-robin mailbox. The dashboard shows each request's
source mailbox as a badge, taken from session ownership, not anything typed.

**Isolation.** Each account has its own state directory
(`<state_dir>/accounts/<account_id>/`): its own request/thread store, audit log
and poll watermark. One mailbox's failed poll is logged against that account and
never stops the others. Redis/RQ and the WebCargo worker stay shared and
account-agnostic.

## Never commit
- `.secrets/` (OAuth tokens) — git-ignored.
- Real mailbox addresses / a real accounts directory — keep it under `.secrets/`.
This `deploy/accounts.example/` template has placeholders only.
