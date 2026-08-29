"""adapters.email

Implements EmailSource twice: FixtureEmailSource reads .eml-style files from a
scenario directory under fixtures/emails/ (the demo default), and
GmailEmailSource reads one narrowly scoped test mailbox over the Gmail API
(Phase 10.3, receive-only).

Implements EmailSink three times: CollectingEmailSink and FileOutboxSink
deliver nothing, and GmailEmailSink actually sends over a *separate*
send-scoped credential (Phase 11). The two Gmail halves share no token and no
scope — the inbound credential cannot send and the outbound one cannot read.
"""

from translog_quote.adapters.email.fixtures import (
    EmailFixtureScenario,
    FixtureEmailSource,
    load_all_scenarios,
    load_fixture_emails,
    load_scenario,
    parse_fixture_email,
)
from translog_quote.adapters.email.gmail import (
    GmailEmailSource,
    GmailMessageMetadata,
    HttpxGmailTransport,
    parse_gmail_message,
)
from translog_quote.adapters.email.gmail_send import (
    GMAIL_SEND_SCOPE,
    SEND_PATH,
    GmailEmailSink,
    GmailSendTransport,
    HttpxGmailSendTransport,
    build_mime,
)
from translog_quote.adapters.email.outbox import CollectingEmailSink, FileOutboxSink

__all__ = [
    "GMAIL_SEND_SCOPE",
    "SEND_PATH",
    "CollectingEmailSink",
    "EmailFixtureScenario",
    "FileOutboxSink",
    "FixtureEmailSource",
    "GmailEmailSink",
    "GmailEmailSource",
    "GmailMessageMetadata",
    "GmailSendTransport",
    "HttpxGmailSendTransport",
    "HttpxGmailTransport",
    "build_mime",
    "load_all_scenarios",
    "load_fixture_emails",
    "load_scenario",
    "parse_fixture_email",
    "parse_gmail_message",
]
