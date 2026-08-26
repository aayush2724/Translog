"""adapters.email

Implements EmailSource: FixtureEmailSource reads .eml-style files from a
scenario directory under fixtures/emails/. A Gmail source and an SMTP sink
(EmailSink) are later work behind their respective ports — neither exists yet.
"""

from translog_quote.adapters.email.fixtures import (
    EmailFixtureScenario,
    FixtureEmailSource,
    load_all_scenarios,
    load_fixture_emails,
    load_scenario,
    parse_fixture_email,
)

__all__ = [
    "EmailFixtureScenario",
    "FixtureEmailSource",
    "load_all_scenarios",
    "load_fixture_emails",
    "load_scenario",
    "parse_fixture_email",
]
