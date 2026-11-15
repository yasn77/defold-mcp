---
type: architecture
title: Architecture overview
description: defold-mcp is a dual-mode (Live Editor / headless) MCP server for the Defold game engine; how the landed subsystems (config, game.project editor, textproto engine, toolchain manager, deps syncer, docs index) layer and where the planned pieces will sit.
tags: [defold, mcp, architecture, overview]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T02:39:02.076Z
sources:
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-f85d2e0cee2fe665bce2ac21
    resource: repo://src/defold_mcp/__init__.py
  - id: openwiki-source-59b94445da06839fcf0abbbe
    resource: repo://src/defold_mcp/bob_runner.py
  - id: openwiki-source-e97c31ef8fcfbd50708b634f
    resource: repo://src/defold_mcp/config.py
  - id: openwiki-source-0ba4ffedd14b8a1eef3a8986
    resource: repo://src/defold_mcp/deps.py
  - id: openwiki-source-96c224729593bfb78f3b9a49
    resource: repo://src/defold_mcp/docs_index.py
  - id: openwiki-source-3c978bb71c1869c84b6a4726
    resource: repo://src/defold_mcp/editor_client.py
  - id: openwiki-source-d57d093dceb0da08744e2de5
    resource: repo://src/defold_mcp/project.py
  - id: openwiki-source-60018a68c712ae4c069cbdcc
    resource: repo://src/defold_mcp/test_runner.py
  - id: openwiki-source-920c288fa775427e5500aec9
    resource: repo://src/defold_mcp/textproto/schemas.py
  - id: openwiki-source-18a04b4bf25ad2fe02e760b4
    resource: repo://src/defold_mcp/toolchain.py
  - id: openwiki-source-c7f35c27a3e804b5af2c8277
    resource: repo://tests/test_textproto_roundtrip.py
generated: { by: "opencode", at: "2026-11-15T02:39:02.076Z" }
---

# Architecture overview

`defold-mcp` is a Python MCP server that gives coding agents a structured, safe way to build, inspect, mutate, compile, test, bundle and live-drive Defold game projects. Eight subsystems are landed; the MCP server surface itself is next.

## What exists today

1. **`src/defold_mcp/config.py` — constants and environment.** Pure, side-effect-free module owning every magic value: artifact source URLs, the hard limits `MIN_BOB_JAVA_MAJOR = 25` (bob.jar 1.14.x needs OpenJDK 25, class file 69.0), `ENGINE_POST_MAX_BYTES = 1024`, `DEFAULT_ENGINE_PORT = 8001`, the six-entry `COMPILED_SETTING_KEYS` tuple, `get_cache_dir()` (`DEFOLD_MCP_CACHE_DIR` override, else `~/.cache/defold-mcp`) and `host_platform_info()` mapping the host onto Defold's platform triplets.

2. **`src/defold_mcp/project.py` — the `game.project` model.** The lossless, line-preserving editor for Defold's project file (see [the game.project INI editor page](game-project-ini-editor.md)) plus `resolve_project_root()`. Imports `COMPILED_SETTING_KEYS` from `config` — the first instance of the intended layering.

3. **`src/defold_mcp/textproto/` — the structured-resource core.** The Protobuf Text Format engine making agent-driven mutation safe: a tolerant parser with recursive nested-`data:` decoding, a canonical byte-exact serializer with `BareIdent` quoting and math default omission, schema tables (documented as unwired until the server-wiring task), and the mutation/collision-shape layer. See [the textproto engine page](textproto-engine.md) and [mutations and collision shapes](mutations-and-collision-shapes.md).

4. **`src/defold_mcp/toolchain.py` — the toolchain manager.** Defold version resolution (channels and pinned specs, TTL cache, pinned-404 GitHub-release fallback; channels fail loudly), atomic `PK`-verified artifact downloads, first-qualifying JDK discovery (portable → `JAVA_HOME` → PATH), and the hardened Adoptium JDK 25 installer. See [toolchain and JDK handling](toolchain-and-jdk.md).

5. **`src/defold_mcp/deps.py` — the `.deps/` syncer.** Dependency-zip and `builtins.zip` extraction into `<project_root>/.deps/` with validate-all-then-write zip-slip protection and the dependency-name sandbox-escape fix, plus `.defignore`/`.gitignore` repair. See [dependency and builtins sync](deps-sync.md).

6. **`src/defold_mcp/docs_index.py` — the documentation FTS index.** SQLite FTS5 search over Defold's `llms` corpus, `ref-doc.zip` namespaces, examples and the Insality Asset Portal feed, with strict real-format ingestion, bm25 + title-boost ranking, stars-first asset ranking and a 25k-capped document reader. See [the documentation FTS index page](docs-index.md).

7. **`src/defold_mcp/editor_client.py` — the Editor HTTP client.** The Live-Editor half of dual-mode operation: probe-based discovery, OpenAPI-negotiated command/preview/console routes, the bearer-token-isolated `/eval` transaction bridge with injection-proof Lua quoting, and the Editor's own `/bob` build route. See [the Editor client page](editor-client.md).

8. **`src/defold_mcp/bob_runner.py` + `test_runner.py` — the headless runners.** `bob.jar` builds with structured diagnostics and build reports, and `dmengine_headless` test runs with `TEST`-event parsing and guaranteed timeout kills — `game.project` never mutated. See [bob and headless test runners](bob-and-test-runners.md).

## Layering rule

Constants and environment resolution live in `config`; artifact-format models live above it; download/version state lives in `toolchain` and `deps`; search state in `docs_index`. Dependencies flow strictly downward: `project`, `textproto`, `toolchain`, `deps`, `docs_index`, `editor_client`, `bob_runner` and `test_runner` may import `config` (and `deps`/runners import `toolchain`), never sideways into each other's internals and never upward. The FastMCP tool layer (`server.py`, not yet written) will sit on top of everything and own the remaining clients (`engine_client`, `bridge_client`). No module imports from a layer above it.

## Target architecture (planned, not yet implemented)

- **Twenty MCP tools** (`defold_doctor` … `defold_docs_search`) over FastMCP (stdio by default, StreamableHTTP optional), each accepting an optional `project_root` resolved via `resolve_project_root()`.
- **Dual-mode transparency:** with the Defold Editor open (`.internal/editor.port`), structured mutations route to undoable `editor.transact()` calls and builds to the Editor's own `bob`; headless, the same operations use the textproto serializer and the cached CLI toolchain. Every response reports its backend.
- **Remaining clients:** Engine Service (TCP logs, binary Protobuf `@resource/reload`, MP4 recording) and `extension-automation-bridge` v2 (live scene, FIFO input, screenshots) — the Editor HTTP client and the bob/test runners are landed.

## Package and entrypoints

`pyproject.toml` declares the package `defold-mcp` (hatchling, `requires-python = ">=3.14"`, deps `mcp>=1.6.0`, `httpx>=0.27.0`, `pillow>=10.0.0`, dev extras `pytest` + `pytest-asyncio`) and reserves the console script `defold-mcp = "defold_mcp.__main__:main"` — the `__main__` module is not yet implemented and is expected by later wiring tasks.
