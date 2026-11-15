# Files

- [Bob and headless test runners](bob-and-test-runners.md) - How defold-mcp runs bob.jar builds (java resolution, diagnostics parsing, build reports) and dmengine_headless test collections (--config overrides, TEST event parsing, guaranteed timeout kills).
- [Automation bridge client](bridge-client.md) - The extension-automation-bridge v2 client — DLIB 500 unwrapping into structured errors, FIFO lease auto-renewal, guard-carrying input actions and screenshot capture with sha256 validation.
- [Dependency and builtins sync](deps-sync.md) - How sync_project_deps unpacks dependency zips and builtins.zip into .deps/ with sandbox-escape rejection and zip-slip protection, and repairs .defignore/.gitignore.
- [Documentation FTS index](docs-index.md) - The SQLite FTS5 index behind defold_docs_search — pinned schema, ingestion of the four corpora in their plan-verified real formats, ranking rules and the doc_id stability contract.
- [Editor HTTP client](editor-client.md) - The Defold Editor HTTP client — probe-based discovery, OpenAPI negotiation, command/preview routes, the token-isolated /eval bridge with injection-proof Lua quoting, and the /bob route.
- [Engine service client](engine-client.md) - The running-engine client — binary Protobuf wire encoding for hot-reload and system commands, the TCP log collector with eviction-safe offsets, and the MP4 recording protocol.
- [game.project INI editor](game-project-ini-editor.md) - How defold-mcp parses and edits Defold's game.project losslessly — the line-list model, compiled-path 'c' suffix enforcement, dependencies#N handling, injection guards and atomic saving.
- [Mutations and collision shapes](mutations-and-collision-shapes.md) - The textproto mutation layer — add_component's embedded/referenced split with guardrails, and PNG-derived box and convex-hull collision shape generation.
- [Architecture overview](overview.md) - defold-mcp is a dual-mode (Live Editor / headless) MCP server for the Defold game engine; how the landed subsystems (config, game.project editor, textproto engine, toolchain manager, deps syncer, docs index) layer and where the planned pieces will sit.
- [Textproto engine](textproto-engine.md)
- [Toolchain manager and JDK handling](toolchain-and-jdk.md) - Defold version resolution with TTL caching, atomic verified artifact downloads, first-qualifying JDK 25 discovery, and the hardened Adoptium JDK installer.
