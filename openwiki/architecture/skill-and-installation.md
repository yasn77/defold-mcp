---
type: architecture
title: The agent skill and installation
description: The portable defold skill package (SKILL.md with the 53-char trigger description plus four reference guides) and the harness registration, handshake probe and timeout guidance.
tags: [defold, skill, installation, mcp, registration]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T15:05:00.624Z
sources:
  - id: openwiki-source-1141fa420136f17e342feb42
    resource: repo://docs/INSTALL.md
  - id: openwiki-source-1bdb44b30bcf598303c0cbc4
    resource: repo://scripts/mcp_handshake_test.py
  - id: openwiki-source-82c3379b61bb0a67f56294d1
    resource: repo://skill/defold/references/proto-schemas-quickref.md
  - id: openwiki-source-e86976ecdb1a581288e2f627
    resource: repo://skill/defold/SKILL.md
  - id: openwiki-source-aa7bb9cc08c41acd52278b2d
    resource: repo://src/defold_mcp/server.py
  - id: openwiki-source-ce941463b68b754d5210fcd3
    resource: repo://src/defold_mcp/textproto/mutations.py
  - id: openwiki-source-ec5f3515eba7d123ca68e770
    resource: repo://tests/test_mcp_stdio_e2e.py
generated: { by: "opencode", at: "2026-11-15T15:05:00.624Z" }
---

# The agent skill and installation

The final phase of the plan delivered two consumables: a **portable agent skill** that teaches any MCP-capable harness how to drive Defold correctly, and the **registration story** that wires the server into those harnesses.

## The skill (`skill/defold/`)

Authored once in the repo (single source of truth) and installed by copy or symlink — `~/.config/opencode/skills/defold` symlinks here.

- **`SKILL.md`** — frontmatter restricted to the portable subset (`name`, `description`, `version`, `license`; harness-specific keys are rejected by strict loaders). The description is exactly `Use when building, testing, or bundling Defold games.` (53 chars) — self-contained as a trigger string, pinned byte-exactly by a validation script. The body: intro → When to Use (+ Don't use for) → Prerequisites → Quick Reference (all 20 bare tool names grouped by purpose, with the harness-prefix note) → Procedure (the 6-step closed-loop workflow, each step with "done when" criteria) → **Pitfalls** (all ten hard invariants — strict Lua 5.1, compile-time collections, hashed ids, the four script contexts, `self` userdata + shared VM state, literal `go.property`, `require` paths, the reachability compiler, `#version 140` shaders, physics scale — plus never-hand-edit-escaped-`data:` and the licence attribution reminder) → Verification (the proof checklist). 13,541 chars under the 18,000 cap.
- **`references/`** (progressive disclosure — the depth lives here): `architecture-and-lua-rules.md` (URL addressing, script-context boundaries, the shared-state trap, Monarch/Druid/DefTest idioms); `shaders-and-materials.md` (a complete `#version 140` sprite pair obeying every invariant it teaches, hardware instancing, view-vs-world normals, the glTF PBR pipeline traced from the real engine shaders); `cross-platform-and-genres.md` (the platform matrix with honest gaps — consoles SDK-gated, no VS Code/Cursor timeout knob — genre recipes, ads/portal extensions); `proto-schemas-quickref.md` (all 14 `defold_mutate_resource` payloads verified 1:1 against the registry, the embedded-vs-referenced shapes, the live-verified bob `game.project` rules).

Every action name, payload key and API call in the skill was verified against the shipped server, the mutations registry and the offline ref-doc index during review — the three drift findings (an invented `gui.property()`, two phantom `runtime_act` actions, a wrong collision mode) were caught and fixed before merge.

## Registration (`docs/INSTALL.md`)

The canonical per-harness guide: the stdio config block for each of Claude Code, Codex CLI, Cursor, VS Code, Gemini CLI, OpenCode, Windsurf and Zed (each with its real config key and shape — OpenCode's `mcp.defold` uses `"type": "local"`, an array `command` and an `environment` block; Zed uses `context_servers`), plus the StreamableHTTP mode (`defold-mcp --transport streamable-http`). **Per-tool timeouts are the critical guidance**: `defold_setup_toolchain`/`defold_bundle`/`defold_test_run` legitimately take minutes, so clients must be raised to ≥ 600 s (900 preferred) — the units differ per harness (OpenCode and Claude Code take milliseconds; Codex takes seconds; VS Code and Cursor expose no knob, documented honestly). Troubleshooting is grounded in the [live toolchain notes](live-toolchain-notes.md): the JDK-25 auto-install, the 428 MB cache, and the never-concurrent-gates rule.

The only harness actually wired is the user's: **OpenCode**, via a backed-up `opencode.json` edit adding exactly one `defold` entry (900,000 ms timeout, `DEFOLD_MCP_CACHE_DIR` pinned), validated by JSON round-trip and a diff against the backup showing a single insertion.

## The handshake probe (`scripts/mcp_handshake_test.py`)

The pre-flight every harness can run: spawns the real binary with a **sanitised environment** (only `PATH`, `HOME`, `USER`, `TMPDIR` — mirroring how harnesses filter MCP server envs), performs the `initialize` handshake (protocolVersion `"2025-06-18"`), asserts exactly 20 tools with descriptions and object schemas, calls `defold_doctor`, enforces stdout purity (every frame parses as JSON), and SIGTERMs cleanly. Negative paths exit non-zero with clear messages (missing binary, non-JSON stdout). Runs as `mise run handshake`.
