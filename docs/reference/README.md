# Frozen source documents

These are the inputs the demo is built from. They are frozen: the implementation
follows them, and any change to requirements is recorded in `docs/architecture.md`
as a decision rather than by editing these files.

| File | What it is |
|---|---|
| `cargo-automation-workflow.pdf` | 11pp, *Cargo Quote Automation — Full Workflow*. Steps 1a–9, the canonical shipment JSON shape, the required-fields checklist, and the WebCargo request/response shapes. **The project specification.** |

## Not committed

Two further sources exist outside this repository and are deliberately not
vendored here:

| Source | Location | Why it is not committed |
|---|---|---|
| The Ahmedabad–Bahrain email thread (10pp) | `~/Downloads/3.pdf` | Contains the real names, email addresses, phone numbers and business addresses of client and Translog staff. Committing it would put third-party personal data into version control. It is the ground truth for extraction and clarification behaviour, so demo fixtures reproduce its *wording and information-arrival pattern* with synthetic identities instead. |
| WebCargo screen recording (103 MB) | `~/Downloads/Screen Recording 2026-07-19 at 12.31.05 PM.mov` | Size, and it is the likely source for resolving AMB-1 (transit time) and AMB-3 (carrier restrictions). Not required by the demo. |

If the team decides the email thread should be versioned, add it here and record
that decision — do not add it silently.
