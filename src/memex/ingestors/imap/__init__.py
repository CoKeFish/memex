"""IMAP ingestor for memex.

Reads config from `sources.config` (with credentials resolved from env vars by
name), polls IMAP folders since the last UID, normalizes each message into a
SourceRecord, and posts to memex via the runner.
"""

from memex.ingestors.imap.source import ImapSource

__all__ = ["ImapSource"]
