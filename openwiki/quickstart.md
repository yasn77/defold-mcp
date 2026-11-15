---
type: quickstart
title: Quickstart
description: Get oriented in defold-mcp in five minutes — what the project is, how to set up the mise/uv environment, run the test gates, and find the subsystem you need to touch.
tags: [defold, mcp, quickstart, onboarding]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T13:47:50.676Z
sources:
  - id: openwiki-source-72b5d686f860ea86c8592080
    resource: repo://mise.toml
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-1370c63fa3e69beb9835d3e9
    resource: repo://src/defold_mcp/__main__.py
  - id: openwiki-source-aa7bb9cc08c41acd52278b2d
    resource: repo://src/defold_mcp/server.py
  - id: openwiki-source-ec5f3515eba7d123ca68e770
    resource: repo://tests/test_mcp_stdio_e2e.py
generated: { by: "opencode", at: "2026-11-15T13:47:50.676Z" }
---

# Quickstart

`defold-mcp` is a Python MCP server (plus a portable agent skill) for the [Defold](https://defold.com) game engine: it gives coding agents structured tools to scaffold, inspect, mutate, compile, test, bundle and live-drive Defold game projects across 2D and 3D genres — safely around Defold's many file-format traps.

The repository is mid-build: everything through the twenty-tool MCP server is landed with tests (the console script `defold-mcp` works); only the live smoke phase, the portable skill and harness registration remain.

## Prerequisites

- **[mise](https://mise.jdx.dev)** — the repo pins its tools in `mise.toml`: **Python 3.14.10** and **uv 0.13.7**. Python ≥ 3.14 is required; mise installs the pinned version automatically, so no system Python setup is needed.
- Nothing else. No Node/npm (deliberately — the server is Python), no Defold installation for unit tests.

First run in a fresh clone:

```bash
mise trust .          # once per clone: the repo ships a mise.toml
mise run test         # creates .venv, installs the package editable, runs the suite
```

`mise run test` depends on `mise run setup`, so it bootstraps itself: it recreates `.venv` with `uv venv --python 3.14` (idempotent via `--clear`) and installs the package with dev extras before running `.venv/bin/pytest -m "not live" -q`.

## Everyday commands

| Command | What it does |
|---|---|
| `mise run test` | The default gate: 265 unit tests, hermetic (no network, no JVM, under 20 seconds). |
| `mise run live` | The live-marked integration tests (real Defold downloads; minutes). |
| `mise run setup` | Just rebuild `.venv` and reinstall (rarely needed directly). |
| `.venv/bin/pytest tests/ -v` | Plain pytest when you want verbose single-file runs. |

## Where things live

- `src/defold_mcp/config.py` — every constant, URL and limit the server will rely on, plus cache-dir and host-platform resolution. Start here to understand the shape of the project's assumptions.
- `src/defold_mcp/project.py` — the lossless `game.project` editor and `resolve_project_root()`. Read the [game.project INI editor](architecture/game-project-ini-editor.md) page before touching it: its whole contract is byte-level losslessness around Defold's conventions (compiled-path `c` suffixes, `dependencies#N`, CRLF preservation).
- `src/defold_mcp/textproto/` — the structured-resource core: the byte-exact Protobuf Text parser/serializer pair that lets agents mutate `.collection`/`.go`/`.gui` resources without hand-escaping. Start from the [textproto engine](architecture/textproto-engine.md) page; the [mutations and collision shapes](architecture/mutations-and-collision-shapes.md) page covers the mutator guardrails and PNG collision generation.
- `src/defold_mcp/toolchain.py` — Defold version resolution (channels/pinned, TTL cache), atomic `PK`-verified artifact downloads, and JDK 25 discovery/install. Start from [toolchain and JDK handling](architecture/toolchain-and-jdk.md).
- `src/defold_mcp/deps.py` — `.deps/` sync: dependency and builtins extraction with zip-slip and sandbox-escape protection, plus ignore-file repair. See [dependency and builtins sync](architecture/deps-sync.md).
- `src/defold_mcp/docs_index.py` — the SQLite FTS5 index over Defold's docs corpus and the Asset Portal feed. See [the documentation FTS index page](architecture/docs-index.md).
- `src/defold_mcp/editor_client.py` — the Live-Editor HTTP client (probe-based discovery, command/preview routes, the token-isolated `/eval` bridge). See [the Editor client page](architecture/editor-client.md).
- `src/defold_mcp/bob_runner.py` + `test_runner.py` — headless builds and test runs (structured bob diagnostics, `TEST`-event parsing, guaranteed timeout kills). See [bob and headless test runners](architecture/bob-and-test-runners.md).
- `src/defold_mcp/engine_client.py` — the running-engine protocol stack (binary Protobuf hot-reload, TCP logs, MP4 recording). See [the engine client page](architecture/engine-client.md).
- `src/defold_mcp/bridge_client.py` — the automation bridge client (live scene, FIFO input, screenshots). See [the automation bridge client page](architecture/bridge-client.md).
- `src/defold_mcp/server.py` + `__main__.py` — the twenty-tool MCP server (stdio default; `defold-mcp --transport streamable-http` for HTTP). See [the MCP server page](architecture/mcp-server.md).
- `src/defold_mcp/templates/` — the four project scaffolding profiles with the Defold licence attribution module. See [templates and the mutations registry](architecture/templates-and-mutations.md).
- `tests/` — one file per module. The suite is the specification of the modules above, including the verified Defold 1.14.1 fixture bytes for the round-trip contract.
- `pyproject.toml` — package metadata, the `defold-mcp` console-script entrypoint (not yet implemented — see the [architecture overview](architecture/overview.md) for what is planned) and the pytest configuration (the `live` marker split and the asyncio fixture-loop pin).

## Conventions to follow when changing code

- **TDD, strictly**: red → green → refactor, and `mise run test` green before anything is merged.
- British English with Oxford spelling in all prose, comments and identifiers that form words (initialise, behaviour, serialise).
- Keep `config.py` the single home for constants; modules below never import from modules above.
- Never hand-edit nested `data:` strings in Defold resource files — go through the textproto engine.
