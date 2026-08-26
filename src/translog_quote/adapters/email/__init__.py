"""adapters.email

Will implement EmailSource and EmailSink.

FixtureEmailSource reads .eml-style files from a scenario directory;
FixtureEmailSink writes to an outbox. A Gmail source and an SMTP sink are later
work behind the same two ports.
"""
