"""Mail as domain data — inbound and outbound message shapes.

These are DTOs, not transport. Nothing here knows about IMAP, SMTP, Gmail or
files; the adapters that do produce and consume these types.
"""

from translog_quote.domain.email.model import OutboundMessage, RawEmail

__all__ = ["OutboundMessage", "RawEmail"]
