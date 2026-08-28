# Phase 10.3 — local setup for the one-email Gmail test

**Scope:** receive one real email from *your own secondary Gmail account* and
push it through the existing extraction and validation pipeline. Nothing is
sent, nothing is modified in Gmail, and the company mailbox is not involved.

Everything below happens on your machine. No credential is committed: the two
OAuth files live under `.secrets/`, which is git-ignored, and your mailbox
address lives in `.env`, which is also git-ignored.

---

## What you configure

| Thing | Where | Committed? |
|---|---|---|
| Test mailbox address | `TRANSLOG_GMAIL__TEST_ADDRESS` in `.env` | no |
| OAuth client file | `.secrets/gmail_client_secret.json` | no |
| OAuth token (written for you) | `.secrets/gmail_token.json` | no |

You are never asked for a Gmail password — not by this project, not by the
command. Consent happens on Google's own sign-in pages.

---

## Steps

### 1. Google Cloud project + Gmail API

1. Open <https://console.cloud.google.com/>, signed in **as the secondary test
   account** (simplest — it makes that account the project owner).
2. Create a project (any name, e.g. `translog-gmail-test`).
3. **APIs & Services → Library →** search "Gmail API" → **Enable**.

### 2. OAuth consent screen

1. **APIs & Services → OAuth consent screen.**
2. A consumer Gmail account only offers **External** — choose it. (A Workspace
   account would offer Internal; either works for this test.)
3. Fill the required fields (app name, your email as support + developer
   contact). Nothing else matters here.
4. Add the scope `https://www.googleapis.com/auth/gmail.readonly` — this is
   the only scope this integration requests, and it makes sending or deleting
   impossible at the credential level.
5. Under **Test users**, add the secondary Gmail address. While the app is in
   "Testing", only listed test users can consent — which is exactly what we
   want, and it means no Google verification review is needed.

### 3. OAuth client

1. **APIs & Services → Credentials → Create credentials → OAuth client ID.**
2. Application type: **Desktop app**.
3. Download the JSON and save it as:

   ```
   .secrets/gmail_client_secret.json
   ```

   (`mkdir -p .secrets` first if needed. The directory is git-ignored.)

### 4. Point the project at your test mailbox

In `.env` (copy from `.env.example` if you do not have one yet):

```
TRANSLOG_GMAIL__TEST_ADDRESS=your.secondary.address@gmail.com
```

The OpenRouter key must also already be set, since the test runs live
extraction:

```
TRANSLOG_OPENROUTER__API_KEY=...
```

### 5. Install the one-time consent dependency

Needed only for the authorization step; the runtime adapter uses `httpx` alone.

```bash
.venv/bin/pip install google-auth-oauthlib
```

### 6. Authorize once

```bash
.venv/bin/python -m translog_quote.interface.demo gmail-auth
```

A browser opens. **Sign in with the secondary test account** and grant the
read-only Gmail permission. Google will warn that the app is unverified —
expected for an app in Testing with you as its only test user; continue.

The token is written to `.secrets/gmail_token.json` with owner-only
permissions. This step is not repeated.

### 7. Send the test email

From the secondary Gmail account, send a normal enquiry email **to that same
account** (or from any account to it — either works). Base it on one of the
dataset enquiry examples, typed as text. Do not attach the original PDF: this
phase reads the message body only.

### 8. Run the test

```bash
.venv/bin/python -m translog_quote.interface.demo gmail-test
```

It reads the single newest message matching `in:inbox`, maps it to `RawEmail`,
runs the existing Qwen extraction and the existing deterministic validation,
and prints the extracted shipment.

---

## What the command can and cannot do

- Reads at most **one** message, from `in:inbox` only (`TRANSLOG_GMAIL__QUERY`,
  `TRANSLOG_GMAIL__MAX_RESULTS`).
- Refuses to read at all if the authorized account is not the address in
  `TRANSLOG_GMAIL__TEST_ADDRESS`.
- Skips drafts and sent-only mail.
- Cannot send, reply, draft, label, or delete: the transport issues GETs only,
  the scope is read-only, and no `EmailSink` is constructed.
- Does not contact WebCargo.

## Troubleshooting

| Message | Meaning |
|---|---|
| `No Gmail test mailbox configured` | `TRANSLOG_GMAIL__TEST_ADDRESS` is unset in `.env` |
| `No Gmail token file at …` | step 6 has not been run |
| `The authorized Gmail account does not match …` | you consented with the wrong account — delete `.secrets/gmail_token.json` and re-run `gmail-auth` |
| `Gmail API refused the request (403)` | consent did not grant `gmail.readonly` |
| `NO MESSAGE FOUND` | nothing in the inbox matched; send the test email and retry |
