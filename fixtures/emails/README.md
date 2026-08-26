# Email fixtures

Raw client email input for the demo — the layer between "a client wrote an
email" and "the extraction adapter reads text" (Phase 5, not built yet).

**Not the same thing as `fixtures/scenarios/`.** That directory (Phase 1) holds
full end-to-end demo bundles per S1–S4 business scenario — emails plus cached
model responses plus rate sets. This directory holds only the email/thread
layer, on its own, independent of which business scenario eventually consumes
it.

## Format

Each `.eml` file is a small header block, a blank line, then a free-text body:

```
Message-Id: <b1@oceanictraders.example>
In-Reply-To: <parent-message-id>        (only on a reply)
References: <parent-message-id>         (only on a reply)
From: Sender Name <sender@example.example>
To: quotes@translogexpress.example
Subject: ...
Date: 2026-09-03T09:45:00+05:30

The email body, written like a normal business email.
```

This is not RFC 5322/MIME — it is a small, hand-readable format carrying
exactly what `RawEmail` needs (`domain.email.RawEmail`), parsed by
`adapters.email.fixtures.parse_fixture_email`. `To:` is written for realism
but has no corresponding `RawEmail` field, so it is read and dropped.

Every address uses the `.example` TLD (RFC 2606, reserved for documentation)
— including Translog's own (`translogexpress.example`, distinct from the real
`translogexpress.com`) — so nothing here resolves to, or could be mistaken
for, a real mailbox. Every name, company and address is fictional.

Files are named `001_initial.eml`, `002_reply.eml`, and so on. Loading sorts
by filename, which is what makes load order deterministic and independent of
filesystem timestamps.

## Scenarios

| Directory | Demonstrates |
|---|---|
| `a_complete_request/` | One email with every required field present, including both conditionals (chemical + MSDS attached, door + address given). Ahmedabad → Bahrain, per the reference example. |
| `b_incomplete_then_clarified/` | Two messages, one thread. The first omits commodity, pieces, chemical status and delivery type; the second (a reply) supplies exactly those four — the same fields Phase 2's merge tests exercise, so this fixture and that test describe the same flow. |
| `d_conflicting_reply/` | Two messages, one thread. The first states 500 kg; the reply says "actually, the cargo is 700 kg." Exists to exercise Phase 2's conflict handling — the fixture does not resolve the conflict, and neither does anything that reads it. |
| `e_chemical_shipment/` | One email, chemical cargo with MSDS explicitly stated as attached — the conditional MSDS rule's *passing* case, complementing scenario B/C's failing case. |
| `f_door_delivery/` | One email, door delivery with an address given — the conditional address rule's passing case. |

## What this layer does not do

It does not extract shipment fields, validate a shipment, merge records,
generate a clarification, or call a model. It only turns fixture files into
`RawEmail` values. Everything past that point belongs to a later phase.

Thread grouping (`EmailFixtureScenario.thread`) is fixture metadata, not the
output of a correlation algorithm — `domain.conversation.CorrelationPolicy` has
no concrete implementation yet. What this layer guarantees is that the
underlying data is correlatable: each reply's `In-Reply-To`/`References`
correctly chains to the message before it, which is what a real policy will
need once it exists.
