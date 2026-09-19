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

## Never commit
- `.secrets/` (OAuth tokens) — git-ignored.
- Real mailbox addresses / a real accounts directory — keep it under `.secrets/`.
This `deploy/accounts.example/` template has placeholders only.
