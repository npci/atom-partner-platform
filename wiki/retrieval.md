# Retrieval

> **Verified at:** commit `4d4999c`, 2026-09-03. Read from
> `backend/app/rag/` — nine modules, ~1,200 lines.
>
> **Do not carry assumptions over from the Authority's retrieval page.** This
> implementation is deliberately and substantially simpler. The differences are
> the most important thing on this page.

## What it is

Two stores, one table. Both the Document RAG and the Code RAG write chunks into
`document_chunks` in pgvector, separated by a `doc_category` column:

| Category | Scope | Populated from |
|---|---|---|
| `change_doc` | One `change_id` | Documents the Authority sent |
| `kb` | Cross-change | The partner knowledge base |
| `code` | One `repo_id` | An indexed GitLab repository |

Embeddings come from the partner's **own** Ollama service —
`nomic-embed-text`, 768 dimensions, landing in `vector(768)` columns. Nothing
is embedded off-site.

## How it differs from the Authority

The Authority runs a heavy retrieval path. This does not, and each reduction is
a stated choice rather than an unfinished port:

| Authority | Here | Why |
|---|---|---|
| Hybrid BM25 + vector, RRF fusion | **Cosine similarity only**, via pgvector's `<=>` | Simplicity; no lexical index to maintain |
| Tree-sitter / AST chunking | **Line-aware bounded windows** with overlap | Works for any language, no per-language grammar |
| Symbol graphs, LSP, multi-pass ingest | **Full re-index each run** | No incremental-state machinery to get wrong |
| Reranking | None | — |

The code chunker states its position plainly: *"Pragmatic — no tree-sitter / AST
parsing (that's the NPCI heavy path); for partner-side retrieval, bounded line
windows over each file are enough and work for any language."*

**If you are forking to improve retrieval quality, this is where the headroom
is.** The interfaces are small and the stores are shared, so adding a lexical
index or a reranker is an additive change rather than a rewrite.

## Fail-soft everywhere

Every layer degrades rather than raising, and this is consistent enough to be a
design rule:

- **Retrieval** returns `[]` on any error — pgvector absent, bad query
  embedding — so an agent answers ungrounded rather than crashing.
- **The embedding cache** treats a missing or broken table as a plain miss.
- **Ingestion** continues past an individual failure.

**This is the property most worth understanding before you trust an answer.**
An agent whose retrieval silently returned nothing still produces confident,
well-structured output. There is no output check anywhere in the platform that
detects ungrounded generation.

The practical consequence, which [`../USER_GUIDE.md`](../USER_GUIDE.md) states
for operators and is worth repeating for developers: **if answers look
ungrounded, verify the corpus is populated before suspecting the model.**

## Two guards worth knowing

**The cache is keyed on `(content_sha256, embedding_model)`** — so changing the
embedding model can never serve a stale vector from the old one. That mistake
is silent and produces gibberish similarity scores, and the composite key is
what prevents it.

**All-zero vectors are never cached.** A hard embed failure produces a zero
vector; caching it would freeze a transient failure into a permanent dead
entry. The cache explicitly refuses them.

A third guard sits in `embeddings.py`: the batch path asserts the response
length matches the batch and falls back to per-item embedding on any mismatch.
Without that check, a short batch response silently misaligns every vector with
the wrong chunk — a corruption that looks like poor model quality rather than a
bug.

## Ingestion is idempotent by deletion

Re-ingesting a scope deletes its prior chunks first, then inserts the fresh
set. Simple, correct, and it means a re-index is always safe to re-run.

Code ingestion is a **full re-index each run** — it deletes and rebuilds rather
than diffing. For a partner-sized repository that is the right trade; for a very
large monorepo it is the first thing you will want to change.

Both ingestion paths use raw SQL through psycopg rather than the SQLAlchemy
pgvector type, binding vectors as text literals cast to `vector`.

## `symbol_usage.py` — closing one specific gap

Not a retrieval module in the normal sense. The code agent plans from
semantically-retrieved excerpts, which means it can change a symbol **without
knowing who else references it.** This module closes that gap pragmatically by
searching the indexed repository through GitLab's own code-search API.

It is explicitly *not* a call graph or AST engine, and it will not find dynamic
or reflective references. Treat it as a check that catches the common case, not
a guarantee.

## Related

- Where the chunks live: [data model](data-model.md)
- What consumes retrieval: [`../ARCHITECTURE.md`](../ARCHITECTURE.md)
- Configuring the corpus: [`../USER_GUIDE.md`](../USER_GUIDE.md)
