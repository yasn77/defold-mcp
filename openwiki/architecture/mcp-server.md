---
type: architecture
title: MCP server and the twenty tools
description: The MCPServer tool layer — twenty defold_* tools, root resolution, the escaping-aware 25k response cap, dialect-safe editor routing, containment and the process registries.
tags: [defold, mcp, server, tools, stdio]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T14:41:05.547Z
sources:
  - id: openwiki-source-1370c63fa3e69beb9835d3e9
    resource: repo://src/defold_mcp/__main__.py
  - id: openwiki-source-aa7bb9cc08c41acd52278b2d
    resource: repo://src/defold_mcp/server.py
  - id: openwiki-source-60018a68c712ae4c069cbdcc
    resource: repo://src/defold_mcp/test_runner.py
  - id: openwiki-source-ec5f3515eba7d123ca68e770
    resource: repo://tests/test_mcp_stdio_e2e.py
generated: { by: "opencode", at: "2026-11-15T14:41:05.547Z" }
---

# MCP server and the twenty tools

`src/defold_mcp/server.py` is the layer the plan commissioned last: `build_server()` registers **exactly twenty `defold_*` tools** (in the plan's §2.3 order, from `defold_doctor` to `defold_docs_search`) on the MCP SDK's server class — note the installed `mcp` 2.3.0 renamed FastMCP to **`MCPServer`** — and `src/defold_mcp/__main__.py` runs it (`defold-mcp` console script): stdio transport by default, `--transport streamable-http --host --port` optionally, all logging on **stderr** (the SDK's stdio transport frames newline-delimited JSON on stdout and even diverts fd 1 to stderr while serving, so stray prints cannot corrupt the wire).

## Global tool conventions

- **Root resolution:** every project-scoped tool resolves `project_root` as explicit argument → `DEFOLD_PROJECT_ROOT` → upward `game.project` search from the working directory.
- **Response cap:** every text response is capped at 25,000 characters by `_truncate`. The over-budget envelope is shrunk **iteratively** — re-dumping and trimming by overflow plus margin — because re-serialising a JSON slice re-escapes (`\"`→`\\\"`) and can expand; the loop converges geometrically, degrades to an empty preview rather than ever exceeding the cap, and always sets `"truncated": true` with a continuation hint.
- **Backend reporting:** project-scoped responses carry `"backend": "editor" | "headless"` (probed per call via `editor_client.probe_editor`); the key is omitted when a payload has no backend notion (e.g. a doctor report with no reachable project).
- **Containment:** `_resource_path` resolves the requested resource against the resolved project root and rejects any path whose *resolved* location escapes it (`is_relative_to`) — blocking `..` traversal **and** symlink escapes for both `defold_inspect_resource` (read) and `defold_mutate_resource` (write). A `..` that resolves *inside* the root is allowed by design.

## Dual-mode mutation routing

`defold_mutate_resource` routes on **payload dialect**, never on op-name coincidence: editor-dialect ops (`add_resource` with `path`+`template`; `set_property` with a string `node`) go to `editor_client.transact_mutations`; the registry dialect (`parent`/`key`/`value` and the other thirteen ops) always takes the AST path — parse, `apply_operations`, serialize, atomic write. This replaced an earlier name-based routing set that would have crashed registry-dialect `set_property` batches against a live Editor.

## Process and log registries

- `_RUNNING` (keyed by resolved root) tracks spawned engines with their stdout pump threads and rings; `defold_run` **stops any live prior entry first** (via the shared `_stop_entry`), so run-run sequences never orphan an engine; `defold_stop` reaps the same way.
- `_slice_ring` mirrors the [TCP log collector's](engine-client.md) eviction-safe offset semantics exactly: monotone totals, absolute offsets, `next_offset` from the *unfiltered* window before filtering — the Phase-5 duplication bug class cannot recur here.
- `_TCP_COLLECTORS` is wired best-effort: `defold_run` fetches engine `/info` and connects a `TcpLogCollector` when a `log_port` is present; `_stop_entry` pops and stops it. `defold_logs` sources: `process`, `tcp`, `editor`, or `auto` (first available in that order).

## Live-phase refinements

The live smoke tightened three server behaviours: `defold_test_run` now **builds the test collection as the archive bootstrap first** (subcollections are flattened and never archived, so the engine can never boot one via `--config` — the build runs through bob `--settings` in a private temp directory) and reports `run`/`status`/`exit_code`/`rejected_events` with a structured build-failure response; `defold_setup_toolchain` **self-heals a stale or malformed docs.db** (`_is_sqlite_database`/`_discard_unusable_db`) and rebuilds empty indexes; and `defold_compile`/`defold_bundle` failure responses carry a `stdout_tail` so agents can self-correct (see [live toolchain notes](live-toolchain-notes.md)).

## Where the behaviour is pinned

`tests/test_mcp_stdio_e2e.py` (14 tests) drives the **real binary** over stdin/stdout: the JSON-RPC handshake (protocolVersion `"2025-06-18"`), the exact-20-tools list in order, the project_init → info → mutate → inspect workflow over real files (byte-exact round-trips asserted), traversal rejection over the live server, cap enforcement on escaping-heavy payloads, dialect routing with a stubbed probe, orphan prevention, log paging, stdout purity, and clean SIGTERM.
