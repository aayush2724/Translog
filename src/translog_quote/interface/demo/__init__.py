"""The four demonstration scenarios.

Each is a script *and* an assertion over the resulting state and audit trail — an
executable specification, not a manual walkthrough. A scenario that stops passing
is a failing test, which is what keeps the demo honest as the code changes.

    S1  complete email -> quotation -> accept
    S2  incomplete -> clarification -> reply -> quotation
    S3  many rates -> filtering -> fastest eligible
    S4  quotation -> rejection
"""
