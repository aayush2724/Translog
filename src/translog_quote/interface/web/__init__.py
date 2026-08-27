"""The client-facing web POC (Phase 9).

A small local web application that presents the existing workflow:

    enquiry -> extraction -> validation -> clarification draft
            -> human approval -> simulated reply -> merge -> revalidation
            -> rate search (mock data) -> selection -> quotation preview

Presentation only. Every decision on that path is made by code that already
exists and is already tested; this package composes, serialises, and serves.
Nothing here sends email, contacts WebCargo, or bypasses an approval gate —
the same guarantees the terminal POC makes, behind a browser instead of a TTY.
"""
