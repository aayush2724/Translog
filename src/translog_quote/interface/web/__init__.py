"""The Translog web interface.

Two views, one server, one shared presentation layer.

**Scripted POC (Phase 9, the default).** Presents the workflow over the
fictional Northgate scenario with a scripted extractor. Nothing sends email,
contacts WebCargo, or bypasses an approval gate.

**Live view (`--live`).** The same visual language over the *real* Gmail
workflow: real inbound mail on a read-only credential, real outbound mail on a
separate send-only credential, live extraction, simulated rates, and the two
human gates. It reuses the pipeline, ports, store, adapters and stages the CLI
uses — `live_session` is wiring and bookkeeping, and implements no business
rule of its own.

Presentation only, in both cases. Every decision on either path is made by code
that already exists and is already tested; this package composes, serialises,
and serves. The browser's APPROVE and DECLINE buttons post one explicit named
decision, and the server-side `QuotationStage` — unchanged — is what sends or
does not send. No credential can reach the browser: the serialisers cannot see
`Settings`, and the static files are committed source with no templating step
to leak into.
"""
