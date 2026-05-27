"""Source type registry.

Lazy resolver from a `source.type` string (as stored in the `sources` table)
to the concrete `Source` class. Implementations live under
`memex.ingestors.<type>/` and import only `memex.core.source` (Protocol +
dataclass) — see ADR-001 for the strict isolation rationale.

The resolver is intentionally lazy to avoid forcing the API process to import
ingestor-specific heavy dependencies (e.g. `imap_tools`) when it never
instantiates concrete sources.
"""

from __future__ import annotations

from memex.core.source import Source


def resolve(source_type: str) -> type[Source]:
    """Return the concrete `Source` class for the given type string.

    Raises KeyError if no implementation is registered. Heavy modules are
    imported only when their type is requested.
    """
    if source_type == "imap":
        from memex.ingestors.imap.source import ImapSource

        return ImapSource
    raise KeyError(f"no Source implementation registered for type={source_type!r}")


def known_types() -> list[str]:
    """List source types currently resolvable. Useful for introspection."""
    return ["imap"]
