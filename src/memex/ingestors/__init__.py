"""External ingestors that read from sources and push to memex via HTTP.

Strictly isolated from memex internals (ADR-001): only imports from
`memex.core.source` (Protocol + dataclass, no I/O) and `memex.logging`.
Never imports `memex.core.inbox`, `memex.db`, or `memex.api.*`.
"""
