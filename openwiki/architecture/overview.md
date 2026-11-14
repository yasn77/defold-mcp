---
type: architecture
title: Architecture overview
description: defold-mcp is a dual-mode (Live Editor / headless) MCP server for the Defold game engine; how the landed modules (config, game.project editor) layer and where the planned subsystems will sit.
tags: [defold, mcp, architecture, overview]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-14T16:49:47.288Z
sources:
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-f85d2e0cee2fe665bce2ac21
    resource: repo://src/defold_mcp/__init__.py
  - id: openwiki-source-e97c31ef8fcfbd50708b634f
    resource: repo://src/defold_mcp/config.py
  - id: openwiki-source-d57d093dceb0da08744e2de5
    resource: repo://src/defold_mcp/project.py
generated: { by: "opencode", at: "2026-11-14T16:49:47.288Z" }
---

# Architecture overview

`defold-mcp` is a Python MCP server that gives coding agents a structured, safe way to build, inspect, mutate, compile, test, bundle and live-drive Defold game projects. The repository is at an early stage: only the foundational layers are landed, with the wider subsystems planned.

## What exists today

Two layers, cleanly separated:

1. **`src/defold_mcp/config.py` — constants and environment.** Pure, side-effect-free module owning every magic value the rest of the server will rely on:
   - Artifact sources: `D_DEFOLD_BASE` (`https://d.defold.com`), `GITHUB_RELEASES_BASE`, `ASSET_STORE_JSON_URL`.
   - Hard limits: `MIN_BOB_JAVA_MAJOR = 25` (Defold 1.14.x `bob.jar` is compiled for OpenJDK 25, class file version 69.0), `ENGINE_POST_MAX_BYTES = 1024` (Engine Service binary Protobuf POST body ceiling), `DEFAULT_ENGINE_PORT = 8001`.
   - `COMPILED_SETTING_KEYS`: the ordered tuple of the six `game.project` settings that Defold stores as compiled `c`-suffixed paths (`bootstrap.main_collection`, `bootstrap.render`, `bootstrap.debug_init_script`, `input.game_binding`, `input.gamepads`, `display.display_profiles`).
   - `get_cache_dir()`: resolves the artifact cache (`DEFOLD_MCP_CACHE_DIR` override, else `~/.cache/defold-mcp`).
   - `host_platform_info()`: maps the running OS/machine onto Defold's host target triplets (`x86_64-linux`, `arm64-linux`, `arm64-macos`, `x86_64-macos`, `x86_64-win32`) plus executable suffixes, raising a descriptive `ValueError` for unsupported combinations.

<!-- openwiki: broken internal link [openwiki/architecture/game-project-ini-editor.md] file "openwiki/architecture/game-project-ini-editor.md" does not exist. Fix the href or restore the target, then delete this comment. -->
2. **`src/defold_mcp/project.py` — the `game.project` model.** The lossless, line-preserving editor for Defold's project file (see [the game.project INI editor page](openwiki/architecture/game-project-ini-editor.md) for its full contract) plus `resolve_project_root()` for locating a project. It imports `COMPILED_SETTING_KEYS` from `config` rather than duplicating it — the `config → project` dependency is the first instance of the intended layering.

## Layering rule

Constants and environment resolution live in `config`; anything that models a Defold artifact format or talks to the outside world lives above it. The direction of dependency is strictly downward: `project` depends on `config`; future clients (`editor_client`, `engine_client`, `bridge_client`, `bob_runner`, `test_runner`) and the textproto subsystem will depend on both, and the FastMCP tool layer (`server.py`) will sit on top of everything. No module ever imports from a layer above it.

## Target architecture (planned, not yet implemented)

The server's design goal — clearly marked as forward-looking; none of this is in the tree yet:

- **Twenty MCP tools** (`defold_doctor` … `defold_docs_search`) over FastMCP (stdio by default, StreamableHTTP optional), each accepting an optional `project_root` and resolving it via `resolve_project_root()`.
- **Dual-mode transparency:** every project-scoped tool probes the Defold Editor's HTTP API (`.internal/editor.port`) when it is open, routing structured resource mutations to undoable `editor.transact()` calls; when no editor is running, the same operations use a deterministic Protobuf Text Format serializer writing directly to disk. Every response reports which backend served it.
- **Toolchain management:** versioned downloads of `bob.jar`, `dmengine`, `dmengine_headless`, `builtins.zip`, `ref-doc.zip` and a portable Temurin JDK 25 under the cache directory, plus a SQLite FTS5 index over Defold's `llms/` documentation and the Asset Portal feed.
- **Clients:** Editor HTTP (commands, previews, `/eval` with the bearer token kept out of model-visible output), Engine Service (TCP logs, binary Protobuf `@resource/reload`, MP4 recording), and the `extension-automation-bridge` v2 (live scene inspection, FIFO input, screenshots).
- **Headless builds and tests:** `bob.jar` for compile/bundle with structured diagnostics, `dmengine_headless` for deterministic test runs.

## Package and entrypoints

`pyproject.toml` declares the package `defold-mcp` (hatchling build, `requires-python = ">=3.14"`, deps `mcp>=1.6.0`, `httpx>=0.27.0`, `pillow>=10.0.0`, dev extras `pytest` + `pytest-asyncio`) and reserves the console script `defold-mcp = "defold_mcp.__main__:main"`. The `__main__` module is not implemented yet — the reserved target is the only declared-but-absent symbol and is expected by later wiring tasks.
