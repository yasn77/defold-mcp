---
type: quickstart
title: Quickstart
description: Get oriented in defold-mcp in five minutes — what the project is, how to set up the mise/uv environment, run the test gates, and find the subsystem you need to touch.
tags: [defold, mcp, quickstart, onboarding]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-14T22:05:56.160Z
sources:
  - id: openwiki-source-72b5d686f860ea86c8592080
    resource: repo://mise.toml
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-e97c31ef8fcfbd50708b634f
    resource: repo://src/defold_mcp/config.py
  - id: openwiki-source-d57d093dceb0da08744e2de5
    resource: repo://src/defold_mcp/project.py
  - id: openwiki-source-c4a7d4413c1e1f9e6ee656b3
    resource: repo://src/defold_mcp/textproto/__init__.py
generated: { by: "opencode", at: "2026-11-14T22:05:56.160Z" }
---

# Quickstart

`defold-mcp` is a Python MCP server (plus a portable agent skill) for the [Defold](https://defold.com) game engine: it gives coding agents structured tools to scaffold, inspect, mutate, compile, test, bundle and live-drive Defold game projects across 2D and 3D genres — safely around Defold's many file-format traps.

The repository is early-stage: the foundation layers (`config`, the `game.project` editor) and the structured-resource core (`textproto`) with their tests are landed; the MCP server itself is being built in phases.

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
| `mise run test` | The default gate: 70 unit tests, hermetic (no network, no JVM, seconds). |
| `mise run live` | The live-marked integration tests (real Defold downloads; minutes). |
| `mise run setup` | Just rebuild `.venv` and reinstall (rarely needed directly). |
| `.venv/bin/pytest tests/ -v` | Plain pytest when you want verbose single-file runs. |

## Where things live

- `src/defold_mcp/config.py` — every constant, URL and limit the server will rely on, plus cache-dir and host-platform resolution. Start here to understand the shape of the project's assumptions.
- `src/defold_mcp/project.py` — the lossless `game.project` editor and `resolve_project_root()`. Read the [game.project INI editor](architecture/game-project-ini-editor.md) page before touching it: its whole contract is byte-level losslessness around Defold's conventions (compiled-path `c` suffixes, `dependencies#N`, CRLF preservation).
- `src/defold_mcp/textproto/` — the structured-resource core: the byte-exact Protobuf Text parser/serializer pair that lets agents mutate `.collection`/`.go`/`.gui` resources without hand-escaping. Start from the [textproto engine](architecture/textproto-engine.md) page; the [mutations and collision shapes](architecture/mutations-and-collision-shapes.md) page covers the mutator guardrails and PNG collision generation.
- `tests/` — one file per module. The suite is the specification of the modules above, including the verified Defold 1.14.1 fixture bytes for the round-trip contract.
- `pyproject.toml` — package metadata, the `defold-mcp` console-script entrypoint (not yet implemented — see the [architecture overview](architecture/overview.md) for what is planned) and the pytest configuration (the `live` marker split and the asyncio fixture-loop pin).

## Conventions to follow when changing code

- **TDD, strictly**: red → green → refactor, and `mise run test` green before anything is merged.
- British English with Oxford spelling in all prose, comments and identifiers that form words (initialise, behaviour, serialise).
- Keep `config.py` the single home for constants; modules below never import from modules above.
- Never hand-edit nested `data:` strings in Defold resource files — go through the textproto engine.
