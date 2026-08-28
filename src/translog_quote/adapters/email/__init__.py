"""adapters.email

Implements EmailSource twice: FixtureEmailSource reads .eml-style files from a
scenario directory under fixtures/emails/ (the demo default), and
GmailEmailSource reads one narrowly scoped test mailbox over the Gmail API
(Phase 10.3, receive-only). An outbound sink that actually sends mail still
does not exist — CollectingEmailSink/FileOutboxSink deliver nothing.
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
from translog_quote.adapters.email.outbox import CollectingEmailSink, FileOutboxSink

__all__ = [
    "CollectingEmailSink",
    "EmailFixtureScenario",
    "FileOutboxSink",
    "FixtureEmailSource",
    "GmailEmailSource",
    "GmailMessageMetadata",
    "HttpxGmailTransport",
    "load_all_scenarios",
    "load_fixture_emails",
    "load_scenario",
    "parse_fixture_email",
    "parse_gmail_message",
]
