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

---

# Phase 11 — the outbound demo (`gmail-quote`)

Everything above stays true. This section adds the **send** half, which is a
second credential with a second scope, granted separately.

## Accounts

| Role | Account | What it does |
|---|---|---|
| Client | a separate Gmail account | sends the enquiry, receives the clarification and the quotation |
| Translog | the account from step 4 above | receives client mail (read-only token), sends mail (send-only token), and receives the internal approval request |

Two Gmail accounts are enough for the demo. The internal approver mailbox can
be the Translog account itself — the review request lands in the same inbox,
and both the Gmail query and the command filter it out of client processing by
its `[TRANSLOG INTERNAL]` subject marker.

## 1. Grant the send scope

Reuses the same Desktop OAuth client from step 3; only the scope and the token
file differ. Add `https://www.googleapis.com/auth/gmail.send` to the consent
screen's scope list first, then:

```
.venv/bin/python -m translog_quote.interface.demo gmail-auth-send
```

Sign in with the **Translog** account. The token is written to
`.secrets/gmail_send_token.json`, which is git-ignored.

After this the project holds two credentials and neither can do the other's
job: the read token cannot send, and the send token cannot read.

## 2. Configure

```
TRANSLOG_GMAIL__SEND_ENABLED=true
TRANSLOG_GMAIL__APPROVER_ADDRESS=<the internal mailbox>
TRANSLOG_GMAIL__QUERY=in:inbox -subject:"[TRANSLOG INTERNAL]"
TRANSLOG_GMAIL__MAX_RESULTS=5
```

`TRANSLOG_GMAIL__SENDER_ADDRESS` defaults to `TRANSLOG_GMAIL__TEST_ADDRESS`.
Set it explicitly only if Translog sends from a different mailbox than it
reads.

`SEND_ENABLED` is off by default. With it off, `gmail-quote` refuses to start
and every other demo falls back to the outbox sink that delivers nothing — a
token file lying around is never enough to make anything send.

## 3. Run the demo

The demo spans two invocations, because the client's reply arrives whenever it
arrives. State is persisted to `runs/state/` (git-ignored) between them.

```
# --- invocation 1 -----------------------------------------------------------
# The client emails an incomplete enquiry to the Translog account, then:
.venv/bin/python -m translog_quote.interface.demo gmail-quote \
    --approved-by "your.name@company"
```

The enquiry is extracted, validated, and a clarification is drafted. Because
you named yourself, the draft is released and **actually emailed to the
client**. The run then ends:

```
STATUS         : clarification_sent / awaiting_client_reply
persisted      : yes — this run's progress survives the process
```

Exit code 0. The process can now exit safely — sending the clarification and
entering the waiting state is the workflow progressing as designed.

Omitting `--approved-by` shows the draft and stops without sending. Nothing is
written to `runs/state/` on that path, so re-running with the flag proceeds
normally.

```
# --- invocation 2 -----------------------------------------------------------
# The client replies. Then:
.venv/bin/python -m translog_quote.interface.demo gmail-quote \
    --approved-by "your.name@company"
```

This run loads the persisted state, reports `1 message(s) already processed in
an earlier run; skipped`, and processes **only the reply** — no second model
call on the enquiry, and no duplicate clarification. The reply correlates by
its RFC `References` chain, merges, and validates. `DemoRateProvider` produces
simulated rates, the fastest eligible is selected, the review packet is emailed
to the approver, and the command stops at a terminal prompt.

Read the packet in the internal mailbox, then type `APPROVE` or `DECLINE`.

- **APPROVE** -> quotation emailed to the client, state `QUOTATION_SENT`, exit 0
- **DECLINE** -> nothing sent to the client, state `MAKER_REJECTED` (terminal), exit 10
- **Ctrl-D / walking away** -> no decision recorded, nothing sent, request stays
  at `PENDING_APPROVAL`, exit 11

A third run with no new mail reports `STATUS: nothing_new`, calls no model and
sends nothing.

### Between rehearsals

Deleting the `runs/state` directory forgets what has already been sent and
starts a fresh demonstration. Do that between practice runs, never mid-run — a
forgotten `QUOTATION_SENT` is what allows a second quotation to reach the
client.

## What the outbound half can and cannot do

| | |
|---|---|
| Send a clarification a named person approved | yes |
| Send a quotation a named person approved | yes |
| Approve anything on its own | **no** — both gates require a named human |
| Send after a decline | **no** — the decline path never reaches the client sink, and `Quotation` cannot be built without an `Approved` |
| Send the same quotation twice | **no**, across processes — the decision is committed to `runs/state/` before it is reported, and the gate refuses a request already at `QUOTATION_SENT` or `MAKER_REJECTED` |
| Read the mailbox with the send token | **no** — the scope is `gmail.send` |
| Send with the read token | **no** — the scope is `gmail.readonly`, and that transport issues only GETs |
| Present simulated rates as real | **no** — the disclosure travels into the client email |
| Contact WebCargo | **no** — `DemoRateProvider` makes no network call |

**Honest limitations.** The persisted store is a pair of JSON files with no
locking, so two concurrent runs against one state directory would race. And
correlation depends on the client's mail software emitting a `References`
chain — Gmail, Outlook and Apple Mail all do, but a reply carrying only
`In-Reply-To` would name our clarification's Message-ID, which the send-only
credential cannot read back, and would start a fresh request rather than
merging.

## Troubleshooting the send path

| Symptom | Cause |
|---|---|
| `Outbound Gmail is disabled` | `TRANSLOG_GMAIL__SEND_ENABLED` is not `true` |
| `No internal approver address configured` | set `TRANSLOG_GMAIL__APPROVER_ADDRESS` |
| `Gmail refused the send (403)` | consent did not grant `gmail.send`, or the `From` address is not the authenticated account |
| `Re-run the gmail-auth-send consent command` | the send token is missing, expired or revoked |
| The run tries to extract a shipment from an approval request | the Gmail query is not excluding `[TRANSLOG INTERNAL]` — see step 2 |
