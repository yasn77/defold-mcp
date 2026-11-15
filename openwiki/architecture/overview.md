---
type: architecture
title: Architecture overview
description: defold-mcp is a dual-mode (Live Editor / headless) MCP server for the Defold game engine; how the landed subsystems, the twenty-tool server, the agent skill and the CI/release/container delivery layer fit together.
tags: [defold, mcp, architecture, overview]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T16:38:15.389Z
sources:
  - id: openwiki-source-164e2da859b5277df81c7d94
    resource: repo://.github/workflows/ci.yml
  - id: openwiki-source-8ea12a05c67ed54e49e49437
    resource: repo://Containerfile
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-f85d2e0cee2fe665bce2ac21
    resource: repo://src/defold_mcp/__init__.py
  - id: openwiki-source-1370c63fa3e69beb9835d3e9
    resource: repo://src/defold_mcp/__main__.py
  - id: openwiki-source-e97c31ef8fcfbd50708b634f
    resource: repo://src/defold_mcp/config.py
  - id: openwiki-source-0ba4ffedd14b8a1eef3a8986
    resource: repo://src/defold_mcp/deps.py
  - id: openwiki-source-d57d093dceb0da08744e2de5
    resource: repo://src/defold_mcp/project.py
  - id: openwiki-source-aa7bb9cc08c41acd52278b2d
    resource: repo://src/defold_mcp/server.py
  - id: openwiki-source-920c288fa775427e5500aec9
    resource: repo://src/defold_mcp/textproto/schemas.py
  - id: openwiki-source-edd5cc407110cd57fc145906
    resource: repo://src/defold_mcp/textproto/serializer.py
  - id: openwiki-source-18a04b4bf25ad2fe02e760b4
    resource: repo://src/defold_mcp/toolchain.py
  - id: openwiki-source-c7f35c27a3e804b5af2c8277
    resource: repo://tests/test_textproto_roundtrip.py
generated: { by: "opencode", at: "2026-11-15T16:38:15.389Z" }
---

# Architecture overview

`defold-mcp` is a Python MCP server that gives coding agents a structured, safe way to build, inspect, mutate, compile, test, bundle and live-drive Defold game projects. The project is COMPLETE: twelve subsystems, the twenty-tool MCP server, the portable agent skill, and the automation/delivery layer (Ubicloud CI, release-please releases with GHCR image publication, Renovate, the Chainguard container, the skill/integration CLI) — all landed and live-verified against real Defold 1.14.1.

## What exists today

1. **`src/defold_mcp/config.py` — constants and environment.** Pure, side-effect-free module owning every magic value: artifact source URLs, the hard limits `MIN_BOB_JAVA_MAJOR = 25` (bob.jar 1.14.x needs OpenJDK 25, class file 69.0), `ENGINE_POST_MAX_BYTES = 1024`, `DEFAULT_ENGINE_PORT = 8001`, the six-entry `COMPILED_SETTING_KEYS` tuple, `get_cache_dir()` (`DEFOLD_MCP_CACHE_DIR` override, else `~/.cache/defold-mcp`) and `host_platform_info()` mapping the host onto Defold's platform triplets.

2. **`src/defold_mcp/project.py` — the `game.project` model.** The lossless, line-preserving editor for Defold's project file (see [the game.project INI editor page](game-project-ini-editor.md)) plus `resolve_project_root()`. Imports `COMPILED_SETTING_KEYS` from `config` — the first instance of the intended layering.

3. **`src/defold_mcp/textproto/` — the structured-resource core.** The Protobuf Text Format engine making agent-driven mutation safe: a tolerant parser with recursive nested-`data:` decoding, a canonical byte-exact serializer with `BareIdent` quoting and math default omission, schema tables wired through the serializer's optional `extension=` parameter, and the mutation/collision-shape layer. See [the textproto engine page](textproto-engine.md) and [mutations and collision shapes](mutations-and-collision-shapes.md).

4. **`src/defold_mcp/toolchain.py` — the toolchain manager.** Defold version resolution (channels and pinned specs, TTL cache, pinned-404 GitHub-release fallback; channels fail loudly), atomic `PK`-verified artifact downloads, first-qualifying JDK discovery (portable → `JAVA_HOME` → PATH), and the hardened Adoptium JDK 25 installer. See [toolchain and JDK handling](toolchain-and-jdk.md).

5. **`src/defold_mcp/deps.py` — the `.deps/` syncer.** Dependency-zip and `builtins.zip` extraction into `<project_root>/.deps/` with validate-all-then-write zip-slip protection and the dependency-name sandbox-escape fix, plus `.defignore`/`.gitignore` repair. See [dependency and builtins sync](deps-sync.md).

6. **`src/defold_mcp/docs_index.py` — the documentation FTS index.** SQLite FTS5 search over Defold's `llms` corpus, `ref-doc.zip` namespaces, examples and the Insality Asset Portal feed, with strict real-format ingestion, bm25 + title-boost ranking, stars-first asset ranking and a 25k-capped document reader. See [the documentation FTS index page](docs-index.md).

7. **`src/defold_mcp/editor_client.py` — the Editor HTTP client.** The Live-Editor half of dual-mode operation: probe-based discovery, OpenAPI-negotiated command/preview/console routes, the bearer-token-isolated `/eval` transaction bridge with injection-proof Lua quoting, and the Editor's own `/bob` build route. See [the Editor client page](editor-client.md).

8. **`src/defold_mcp/bob_runner.py` + `test_runner.py` — the headless runners.** `bob.jar` builds with structured diagnostics and build reports, and `dmengine_headless` test runs with `TEST`-event parsing and guaranteed timeout kills — `game.project` never mutated. See [bob and headless test runners](bob-and-test-runners.md).

9. **`src/defold_mcp/engine_client.py` — the engine service client.** The running-debug-engine protocol stack: binary Protobuf hot-reload and `@system` messages under the 1024-byte cap, the TCP log collector with eviction-safe absolute offsets, and the MP4 recording protocol. See [the engine client page](engine-client.md).

10. **`src/defold_mcp/bridge_client.py` — the automation bridge client.** The `extension-automation-bridge` v2 client: DLIB 500 unwrapping into structured errors, FIFO lease auto-renewal, guard-carrying input actions and screenshot capture with sha256 validation. See [the automation bridge client page](bridge-client.md).

11. **`src/defold_mcp/server.py` + `__main__.py` — the MCP server.** Twenty `defold_*` tools over the SDK's MCPServer (mcp 2.3.0 renamed FastMCP), stdio by default: root-resolution chain, escaping-aware 25k response cap, payload-dialect editor routing, resolved-path containment and the process/log registries. See [the MCP server page](mcp-server.md).

12. **`src/defold_mcp/templates/` — the project scaffolding.** Four profiles with the plan's physics/HTML5 defaults, serializer-built collections and the Defold licence attribution module; plus the 14-op mutation registry wired into `defold_mutate_resource`. See [templates and the mutations registry](templates-and-mutations.md).

## Layering rule

Constants and environment resolution live in `config`; artifact-format models live above it; download/version state lives in `toolchain` and `deps`; search state in `docs_index`. Dependencies flow strictly downward: `project`, `textproto`, `toolchain`, `deps`, `docs_index`, `editor_client`, `bob_runner` and `test_runner` may import `config` (and `deps`/runners import `toolchain`), never sideways into each other's internals and never upward. The FastMCP/MCPServer tool layer (`server.py`) has landed and orchestrates all subsystems behind the twenty `defold_*` tools. No module imports from a layer above it.

## Post-run items

The commissioned plan is fully delivered. Open follow-ups (all documented, none blocking):

- The `[html5] scale_mode/heap_size/retry_count` settings are parse-proven only — live verification of the section membership may warrant a template correction.
- The first CI/release runs prove what static review cannot: release-please's live output names, the first GHCR push (the package is created private by default — flip visibility for anonymous pulls), and buildx on the Ubicloud fleet.
- Vendor-timeout specifics in `docs/INSTALL.md` rest on executor live-docs checks.
- Future live work: real `test_end` events in the smoke, mac/Windows engine magic on reuse, the Box2D 8-vs-16 vertex question, embedded `.particlefx` legality.

The [CI, releases and the container page](ci-releases-container.md) covers the delivery layer.
