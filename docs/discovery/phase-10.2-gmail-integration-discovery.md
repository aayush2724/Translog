# Phase 10.2 — Gmail integration discovery

**Date:** 2026-08-27 · **Status:** discovery only — no application code changed
**Question:** exactly how will Translog receive real Gmail messages, correlate
replies, create clarification drafts, and send an approved response while
preserving the existing human-approval boundary?

Grounded in the official Gmail API documentation (sending, sync, scopes,
error-handling, and quota guides at `developers.google.com/workspace/gmail`).

---

## 1. Where the adapter plugs in (existing architecture, unchanged)

| Existing piece | Role | Gmail impact |
|---|---|---|
| `EmailSource.fetch_new() -> tuple[RawEmail, ...]` | inbound port | `GmailEmailSource` implements it; nothing upstream changes |
| `RawEmail` (message_id, from, subject, body_text, received_at, in_reply_to, references) | canonical inbound message | Gmail messages map onto it exactly; **no new domain type needed** |
| `CorrelationPolicy.correlate(email, threads)` | reply-to-request matching; refuses to guess (AMB-11) | gets its first concrete, provider-free implementation |
| `ClarificationWorkflow.approve_clarification` | the human gate; **the only caller of `EmailSink.send`** | unchanged — the real sink slots in behind it |
| `EmailSink.send(OutboundMessage)` | outbound port | `GmailEmailSink` implements it |
| `OutboundMessage` (to, subject, body_text, in_reply_to) | approved outbound draft | sufficient; the adapter derives Gmail threading from `in_reply_to` |
| Audit events (`EMAIL_RECEIVED`, `CLARIFICATION_DRAFTED/APPROVED/SENT`) | evidence trail | already cover the flow |
| `StateMachine` (`NEEDS_INFO → CLARIFICATION_SENT`) | send transition | already models it; double-approval already raises |

## A. Recommended architecture

```
Gmail mailbox
   │  (poll: history.list partial sync; full sync on first run / stale historyId)
   ▼
GmailEmailSource ──→ RawEmail ──→ CorrelationPolicy ──→ ClarificationWorkflow.handle
                                                              │ (drafts, waits)
                                          human approves ─────┤
                                                              ▼
                                     GmailEmailSink ←── approve_clarification
                                          │  (RFC 2822 reply, threadId, send)
                                          ▼
                                     Gmail messages.send
```

- **Polling first.** `users.watch` push needs Cloud Pub/Sub plus a reachable
  endpoint and ~7-day watch renewal — infrastructure the company hasn't
  chosen. Polling `history.list` costs 2 quota units per call against a
  6,000-unit/user/minute budget; a 60-second poll is negligible. Push is a
  later optimization, not a requirement.
- **Direct send, not Gmail drafts.** The draft lives in our domain
  (`ClarificationMessage`) behind our gate; creating Gmail-side drafts would
  add the `gmail.compose` scope and a second, weaker draft state.

## B. Authentication — two officially supported options; company must choose

1. **Google Workspace service account + domain-wide delegation** (recommended
   *if* the mailbox is Workspace): headless server auth; a Workspace admin
   grants the exact scopes to the service-account client ID; the adapter
   impersonates the one company mailbox. No interactive consent, no
   refresh-token lifecycle tied to a person.
2. **OAuth 2.0 user consent + refresh token** (required if it is consumer
   Gmail): one interactive consent, then stored refresh token. Revocable by
   the user; token storage becomes an operational secret.

Both `gmail.readonly` (Restricted) and `gmail.send` (Sensitive) trigger
Google's app-verification regimes for *external* apps; an **internal**
Workspace app avoids the restricted-scope security assessment — one more
reason the Workspace question must be answered first. **Do not assume which
model the company uses.**

## C. Minimum scopes (least privilege)

| Purpose | Scope (exact string) | Class |
|---|---|---|
| Read incoming messages | `https://www.googleapis.com/auth/gmail.readonly` | Restricted |
| Send approved messages | `https://www.googleapis.com/auth/gmail.send` | Sensitive |

Explicitly **not** requested: `gmail.modify` (broad read/write),
`gmail.compose` (Gmail-side drafts — ours are internal). Consequence of
read-only: we cannot label or mark-read messages in Gmail; processed-state
lives in our own store (which the design already prefers). If the company
wants Gmail-side "processed" labels, that is a scope-widening decision for
them to make — default no.

## D. Inbound flow (per official sync guide)

1. First run: full sync — `messages.list` (scoped query, e.g. `in:inbox`),
   `messages.get` each, cache the newest `historyId`.
2. Steady state: `history.list(startHistoryId=…)` → `messagesAdded` →
   `messages.get(format=full)`.
3. `historyId` too old (history kept ~1 week+) → API returns **404** → full
   resync; this is a normal path, not an error.
4. Parse to `RawEmail`: walk the MIME `payload.parts` tree; prefer the
   `text/plain` part (base64url-decoded); headers give `Message-ID`,
   `In-Reply-To`, `References`, `From`, `Subject`, `Date` (fall back to
   `internalDate`). HTML-only messages are converted to plain text by
   stripping markup — HTML is untrusted input and is never rendered or
   interpreted. Quoted history and signatures pass through as text; the
   extraction layer already reads free text and needs to know nothing about
   Gmail.
5. Skip our own outbound (`SENT` label / `From` = our mailbox) so replies we
   send are never re-ingested as client enquiries.

## E. Outbound approved-send flow (gate preserved)

Unchanged up to the port: deterministic draft → `NEEDS_INFO` → human reviews
→ `approve_clarification(by=…)` records who/when (`Approved`, audit events)
and only then hands `OutboundMessage` to the sink. The Gmail sink then:

1. Locates the parent by RFC id: `messages.list q="rfc822msgid:<in_reply_to>"`
   → parent `threadId` and its `References` chain.
2. Builds an RFC 2822 reply: `To`, matching `Re:` subject, `In-Reply-To`,
   `References` = parent references + parent Message-ID.
3. Sends base64url `raw` + `threadId` via `messages.send` — the documented
   threading rule: threadId supplied, `Subject` matches, `References`/
   `In-Reply-To` per RFC 2822.

There is structurally no AI→send path: the sink is only reachable through
`approve_clarification`, which requires a named approver, and edited drafts
still pass through the same gate.

## F. Thread correlation (AMB-11 recommendation — not yet implemented)

- **Primary: RFC headers.** `In-Reply-To`/`References` matched against the
  known `Thread.message_ids` — the domain already carries exactly this data,
  provider-free.
- **Corroboration: Gmail `threadId`,** held as adapter/store metadata (not a
  domain field). Agreement → correlate. Disagreement, or no match → the
  existing refusal: `NewRequest` / manual review. **Never subject text, never
  sender+time guessing, never threadId alone** (Gmail's own threading can
  group by subject heuristics; the reference thread's subject accumulated
  four titles).
- The disagreement rule needs stakeholder sign-off before the policy is
  implemented.

## G. Attachments (deferred by design)

Gmail exposes per-part `filename`, `mimeType`, `body.size`, and
`body.attachmentId`; content comes from `messages.attachments.get`
(base64url). For the first integration: **record metadata only, download
nothing.** Note the semantic guard: `msds_attached` currently records what
the client *stated in text*; deriving it from attachment presence is a new
business rule that needs sign-off first. When MSDS handling is approved:
allowlist `application/pdf`, enforce a size cap, sanitize filenames (no path
traversal), parse in isolation, never execute, store temporarily with expiry.

## H. Idempotency / duplicates

| Risk | Guard |
|---|---|
| Same email processed twice | processed-watermark on immutable Gmail `message.id` + `historyId` checkpoint; RFC `Message-ID` dedupe at thread level |
| Own outbound re-ingested | `SENT`/`From` filter (D.5) |
| Clarification sent twice | already structural: the pending draft is popped at approval; a second approve raises `IllegalTransition` |
| Retry after ambiguous send | generate our own `Message-ID`; before any resend, search `rfc822msgid:` to check whether the first send landed |

**Honest limitation:** the current store is in-memory (`InMemoryStore`); the
watermark and processed-set do not survive a restart. Production idempotency
needs a durable store — a real requirement to schedule, not something to
pretend is solved.

## I. Errors and retries (official categories → existing taxonomy)

| Gmail API | Meaning | Maps to | Retry |
|---|---|---|---|
| 400 | malformed request | `ContractViolation` | no |
| 401 | invalid/expired credentials | `PermanentFailure` (auth) | one token refresh, then stop — never loop |
| 403 | quota exceeded / domain policy | `PermanentFailure` with category | policy: no; daily quota: long backoff |
| 404 | missing message / stale `historyId` | full-resync path | n/a — normal control flow |
| 429 | per-user rate/sending limits | `TransientFailure` | exponential backoff per Google guidance (≥1s, doubling, cap 32–64s) — the existing OpenRouter transport pattern already matches |
| 500/502/503/504 | server errors | `TransientFailure` | same bounded backoff |

Quota reality check: 6,000 units/user/min; `messages.get` 20, `list` 5,
`send` 100, `history.list` 2 — this workflow is orders of magnitude below any
limit.

## J. Security requirements

- Credentials/tokens only via `SecretStr` settings and files outside the
  repo (git-ignored path or a secret manager — company infra decision);
  never in source, git, logs, prompts, exceptions, or frontend snapshots.
- Least-privilege scopes (C); internal-app consent screen; documented
  rotation procedure for the SA key or OAuth client secret.
- Email content is untrusted input: never executed, HTML never rendered
  (the web UI is already textContent-only), attachments deferred; prompt
  injection is mitigated structurally — the model can only report field
  values, and deterministic code decides everything downstream.
- Logs/audit carry ids, counts, states, and failure categories — never
  bodies, addresses beyond what the audit design already permits, or
  credentials echoed from provider errors.
- TLS is the only transport (Google API endpoints).

## K. Company prerequisites (exact checklist)

1. Is the mailbox **Google Workspace or consumer Gmail**? (decides B)
2. A Translog-owned **Google Cloud project** with the Gmail API enabled.
3. The auth artifact: **service account + DWD grant** of exactly
   `gmail.readonly` + `gmail.send` to the SA client ID, **or** an OAuth
   client + one-time consent for the mailbox.
4. The **mailbox address** to watch, and written permission to read it.
5. A **test mailbox** and approved test recipients for the smoke test —
   never the live client-facing inbox first.
6. Decisions: Gmail-side labels (would widen scope — default no); push vs
   polling (default polling); the correlation disagreement rule (F).
7. Written authorization to send test emails.

## L. Files/classes to create at implementation time (none created now)

| Location | Contents |
|---|---|
| `config/settings.py` | `GmailSettings` (auth mode, mailbox, credential path as `SecretStr`/path, poll interval) + explicit `email mode: fixture \| gmail` switch — fixture stays the default |
| `adapters/email/gmail.py` | `GmailEmailSource`, `GmailEmailSink`, a small authenticated transport with the bounded-backoff pattern, MIME→`RawEmail` parsing |
| `domain/conversation/` | first concrete `CorrelationPolicy` (header-chain primary; provider corroboration passed in as data) |
| `bootstrap.py` | `build_email_source`/`build_email_sink` switching on explicit config — real mode never activates implicitly |
| `pipeline/` or `interface/` | a small ingestion driver: `fetch_new → correlate → workflow.handle` |
| `tests/unit/` | offline: MIME parsing, threading-header construction, dedupe, error mapping, own-message filtering, gate regression |

Dependency decision deferred: `google-api-python-client`/`google-auth` vs
plain `httpx` against the REST API (httpx is already a dependency; Google's
libraries buy token refresh). Decide at implementation, not now.

## M. Remaining ambiguities

1. Workspace vs consumer Gmail (blocks the auth design).
2. AMB-11 disagreement rule — needs stakeholder sign-off.
3. Durable store for idempotency watermarks (in-memory today).
4. HTML-only email conversion policy (strip-to-text proposed).
5. Attachment/MSDS business rule (stated-in-text vs attached-in-fact).
6. Push vs polling, and Gmail-side labeling — both default to "no/simplest"
   unless the company asks otherwise.
