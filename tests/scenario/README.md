# Scenario tests

The four demonstrations, end to end through `bootstrap`, each asserting over the
resulting state and audit trail.

| Scenario | Proves |
|---|---|
| S1 complete → quotation → accept | happy path, both human gates |
| S2 incomplete → clarification → reply → quotation | BR-8 merge, BR-9 batching, the loop |
| S3 many rates → filtering → fastest eligible | BR-1, BR-2, BR-4, BR-5, BR-6 |
| S4 quotation → rejection | terminal `DECLINED` (AMB-4) |

Empty until the stages they exercise exist. The earliest any of these can run is
Phase 13, which wires the full pipeline end to end; each depends on every phase
before it.
