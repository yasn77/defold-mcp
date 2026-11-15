---
type: architecture
title: Documentation FTS index
description: The SQLite FTS5 index behind defold_docs_search — pinned schema, ingestion of the four corpora in their plan-verified real formats, ranking rules and the doc_id stability contract.
tags: [defold, docs, sqlite, fts5, search]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T01:05:39.762Z
sources:
  - id: openwiki-source-96c224729593bfb78f3b9a49
    resource: repo://src/defold_mcp/docs_index.py
  - id: openwiki-source-4100c25666ae22e213937002
    resource: repo://tests/test_docs_fts.py
generated: { by: "opencode", at: "2026-11-15T01:05:39.762Z" }
---

# Documentation FTS index

`src/defold_mcp/docs_index.py` implements the offline search layer that `defold_docs_search` will serve: one SQLite database (`<cache>/docs.db`, overridable via `DEFOLD_MCP_CACHE_DIR`) holding Defold's documentation corpus and the community Asset Portal, queryable in milliseconds with no network.

## The four corpora and their real formats

The ingestion contracts were corrected during phase 3 to the plan-verified formats after an orchestrator probe showed the first implementation would have indexed **zero** documents from real data (fixtures had co-evolved with wrong shapes — the second occurrence of that failure mode in this project):

1. **Manuals** (`llms/manuals/*.md`) — H1 is the title, body is the markdown; scope `manuals`.
2. **API references** — from `ref-doc.zip`'s per-namespace documents, `{"info": {"namespace": "go", "language": "lua"}, "elements": [{"name": "go.animate", "signature": …, "description": …, "parameters": […], "examples": […]}]}`. One doc per element; `language` `lua` → `lua_api`, `cpp`/`c++` → `cpp_api`. The body is formatted Markdown (signature, parameters, examples). Ingestion accepts a namespace document (dict/JSON/bytes) or the whole zip. `llms/apis/<slug>.md` markdown is also ingested with a best-effort scope heuristic: `KNOWN_EXTENSIONS` slugs → `ext_api`, `dm`/`Dm`-prefixed → `cpp_api`, else `lua_api`.
3. **Examples** (`llms/examples/**`) — scope `examples`.
4. **Asset Portal** — Insality's feed `https://insality.github.io/asset-store/dependencies_store.json`, strictly `{"items": […]}` (any other shape raises `ValueError` naming the expected feed — silent shape tolerance is deliberately rejected). One doc per item; `unlisted: true` and `id == "scene3d"` are **excluded at ingestion**; `url` is the last `content` entry (the latest zip), else the `api` link.

The full live corpus arrives as the GitHub tarball `https://codeload.github.com/defold/defold.github.io/tar.gz/refs/heads/master`; `ingest_llms_tarball` streams it through `tarfile`, consuming only `*/llms/*` members (no disk extraction, no `git` subprocess) and skipping members whose slug contains a `..` component.

## Schema and ranking

The pinned FTS5 virtual table:

```sql
CREATE VIRTUAL TABLE docs_fts USING fts5(
    doc_id UNINDEXED, scope UNINDEXED, title, namespace UNINDEXED,
    tags UNINDEXED, summary, body, stars UNINDEXED, url UNINDEXED,
    tokenize='porter unicode61')
```

- **Search** (`search(query, scope, limit)`): queries are passed as quoted phrases to `MATCH` — hostile input (quotes, `*`, `NEAR`, `AND`/`OR`, CJK) cannot raise or inject. Ranking is `bm25()` with a title-match boost so an exact qualified name ranks first (`search("go.animate", scope="lua_api")` → first hit titled `go.animate`). Scope filters (`manuals`, `lua_api`, `cpp_api`, `ext_api`, `examples`, `assets`; unknown scope → `ValueError`).
- **Assets rank by stars**: matching items order by `CAST(stars AS INTEGER) DESC, bm25()` — the plan's "ranked by stars" rule.
- **Porter stemming** is behavioural-tested both directions (index "Animate", query "animating" matches).
- **`get_doc(doc_id, offset, limit)`**: full-document retrieval capped at 25,000 characters with `truncated`/`total_len` and offset pagination; `offset` and `limit` are clamped (negative offset → 0, zero limit → 1). Unknown id → `KeyError`.

## Stability contract

`doc_id`s are deterministic (`<scope>:<identity>`, e.g. `lua_api:go.animate`, `assets:monarch`) and ingestion upserts (DELETE + INSERT), so re-ingestion replaces rather than duplicates — pinned by a re-ingest test asserting unchanged counts. `stats()` reports per-scope counts.

## Carried-forward risk

llms-apis markdown (`<scope>:<slug>`, e.g. `lua_api:go`) and ref-doc elements (`<scope>:<element>`) share the doc_id space; a symbol like `go.animate` collides only if an apis markdown file is named exactly that (it is not — apis files are per-namespace), but the wiring phase must confirm the one-doc-per-symbol policy and the ingest ordering.
