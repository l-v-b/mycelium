"""Write-path coordination for mycelium.

Writes happen synchronously inside the MCP call in all deployment modes
(pgvector backend): disk write + pgvector upsert. The `direct` module is the
write path; `dedupe` and `frontmatter` are its helpers. (The old `queued`
module + writer worker were removed when storage moved to synchronous pgvector
in v2.3.0.)
"""
