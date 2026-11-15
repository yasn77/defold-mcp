---
type: architecture
title: Dependency and builtins sync
description: How sync_project_deps unpacks dependency zips and builtins.zip into .deps/ with sandbox-escape rejection and zip-slip protection, and repairs .defignore/.gitignore.
tags: [defold, deps, builtins, zip, security]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T01:05:39.762Z
sources:
  - id: openwiki-source-0ba4ffedd14b8a1eef3a8986
    resource: repo://src/defold_mcp/deps.py
  - id: openwiki-source-aa918e62d579257ce43a1ddf
    resource: repo://tests/test_deps_sync.py
generated: { by: "opencode", at: "2026-11-15T01:05:39.762Z" }
---

# Dependency and builtins sync

`src/defold_mcp/deps.py` populates `<project_root>/.deps/` — the directory that makes `/builtins` paths and library code readable on disk for agents and build tools. It exists because Defold projects declare third-party dependencies as archive URLs in `game.project` (`dependencies#0 = https://…/monarch/archive/refs/tags/v1.0.zip`), and the engine's `builtins.zip` (892 KB) holds the built-in materials, shaders and manifests that `/builtins/...` resource references resolve against. Downloading the 250 MB Editor zip just for builtins is explicitly avoided.

## `sync_project_deps(project_root, …)`

1. Loads `game.project` via [`GameProject`](game-project-ini-editor.md) and reads the dependency URLs (`get_dependencies()` — indexed `dependencies#N` keys, legacy comma lists).
2. For each dependency URL: downloads the zip (through `toolchain.download_artifact`, which enforces the `PK` magic bytes and atomic writes), then unpacks into `.deps/<name>/` with `extract_zip`'s protections. The temporary archive is removed in a `finally` block.
3. Resolves the Defold version (`stable` by default) and downloads `builtins.zip` from `https://d.defold.com/archive/{sha1}/engine/share/builtins.zip` (GitHub-release fallback when the version has no sha1), unpacking into `.deps/builtins/`.
4. Repairs the ignore files (below) and returns `{"dependencies": [names…], "builtins": True, "ignore_fixed": bool}`.

## Dependency-name derivation and the sandbox-escape fix

`_dependency_name(url)` takes the last URL path segment minus a `.zip` suffix. **Anything unusable falls back to a 12-hex-character SHA-1 hash of the URL:** the empty segment, `.` and `..`, or any segment containing `/` or `\`. The `..` case is a fixed security hole — a dependency URL ending in `/..` previously produced the name `..`, so extraction targeted `.deps/..` = the project root, and a crafted `game.project` could overwrite `.git/hooks/` (remote code execution on the next git operation in that clone). The escape was found by review probe in phase 3 and is pinned by regression tests asserting hash-named extraction and a clean project root.

## `extract_zip` — validate-all-then-write

The single extractor used for dependencies, builtins and later `ref-doc.zip`:

- **Zip-slip protection:** every member name is checked BEFORE anything is written — absolute paths (leading `/` or `\`) and `..` path components raise `ValueError` naming the offending entry, and each member's resolved target must satisfy `resolved.is_relative_to(dest.resolve())` as a second, containment-level backstop. A rejected zip writes nothing.
- **`strip_root` exact-name semantics:** when set (dependency name, or `"builtins"`), the single top-level directory is stripped only if EVERY non-empty entry shares that exact name — a blanket single-root strip would corrupt a real multi-root builtins zip.
- Directory members create directories; file members stream through `.tmp` + `os.replace`.

## Ignore-file repair

- `/.deps` is appended to `.defignore` (created if missing) so Defold's resource compiler never treats `.deps` as project content, and to `.gitignore`.
- A bare `builtins` line in `.gitignore` (exactly `builtins`, no leading slash — a legacy over-match) is rewritten to `/builtins`; a pre-existing `/builtins` line is left alone, keeping the repair idempotent.

All behaviour is pinned hermetically in `tests/test_deps_sync.py` (22 tests across this module and the toolchain): end-to-end sync with in-memory zips over `httpx.MockTransport`, both zip-slip forms, the sandbox-escape regressions, legacy-only dependency rewrites, empty projects, and ignore-file repair assertions.
