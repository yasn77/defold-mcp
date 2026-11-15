---
type: operations
title: Live toolchain notes (verified against 1.14.1)
description: The fifteen real-world findings from the live smoke — d.defold.com channel semantics, redirects, JDK extraction, bob's game.project parsing rules, the c-suffix asymmetry, engine log prefixes and the settings-file build route.
tags: [defold, bob, toolchain, live, verification]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T14:41:05.547Z
sources:
  - id: openwiki-source-72b5d686f860ea86c8592080
    resource: repo://mise.toml
  - id: openwiki-source-59b94445da06839fcf0abbbe
    resource: repo://src/defold_mcp/bob_runner.py
  - id: openwiki-source-aa7bb9cc08c41acd52278b2d
    resource: repo://src/defold_mcp/server.py
  - id: openwiki-source-2c7c1fca6695097aaa0e289a
    resource: repo://src/defold_mcp/templates/__init__.py
  - id: openwiki-source-60018a68c712ae4c069cbdcc
    resource: repo://src/defold_mcp/test_runner.py
  - id: openwiki-source-18a04b4bf25ad2fe02e760b4
    resource: repo://src/defold_mcp/toolchain.py
  - id: openwiki-source-aa918e62d579257ce43a1ddf
    resource: repo://tests/test_deps_sync.py
  - id: openwiki-source-ec5f3515eba7d123ca68e770
    resource: repo://tests/test_mcp_stdio_e2e.py
  - id: openwiki-source-6adafae9b5e5047999e93399
    resource: repo://tests/test_templates.py
generated: { by: "opencode", at: "2026-11-15T14:41:05.547Z" }
---

# Live toolchain notes (verified against 1.14.1)

The live smoke (`@pytest.mark.live test_live_defold_1_14_1_build_and_headless_test` in `tests/test_mcp_stdio_e2e.py`) drove the real server against the real Defold 1.14.1 toolchain and falsified fifteen plan assumptions. Every item below is verified operational knowledge, with the fix location in parentheses.

## Version resolution (`toolchain.py`)

1. **`d.defold.com/{version}/info.json` does not exist** — only channels are served. Pinned specs resolve by matching channels; the matching channel's sha1 is adopted and cached under the pin (`_match_channel_version`).
2. **`beta` and `stable` can carry the same version with different sha1s** — the preference order is `stable → beta → alpha`, locked by a two-matching-channels test that proves beta is never consulted when stable matches (`_CHANNEL_PREFERENCE`).
3. **Every download host redirects** — `d.defold.com/archive` 301, GitHub assets 302, Adoptium API 307. Both default clients set `follow_redirects=True` (`toolchain._client_or_default`, `docs_index._get_bytes`).

## JDK and cache (`toolchain.py`)

4. **JDK tar extraction loses exec bits** if modes aren't applied — `java -version` → `Permission denied`. `_apply_mode` preserves archive modes on both branches; a non-executable `bin/java` counts as not-installed and re-installs.
5. **Artifacts are reused** when present (every tool call used to re-fetch ~300 MB); magic bytes re-verify on reuse — `PK` for jars/zips, `b"\x7fELF"` for Linux engines (`_engine_magic`), riding the corrupt-artifact re-download path.

## bob and `game.project` (`templates/__init__.py`, `bob_runner.py`)

6. A stale **empty docs.db** made setup skip the rebuild — `_docs_db_needs_build` rebuilds zero-document indexes and self-heals malformed files (`_is_sqlite_database`, `_discard_unusable_db`).
7. **bob REJECTS comment lines** in `game.project` (`;` and `#` alike): `SEVERE Could not parse`. Templates emit none.
8. **`bootstrap.debug_init_script = /builtins/scripts/debug_init.scriptc` is invalid** — that path does not ship in 1.14.1 (only `socket.lua` + LICENSE live there). The setting stays unset.
9. **`input.game_binding` is required** — the builtins default `game_binding = /builtins/input/all.input_bindingc` covers headless projects.
10. **`game.project` resource values are COMPILED paths** — bob strips the final character, so the `c` suffix is mandatory (`all.input_binding` → resolved as `all.input_bindin` without it).
11. **`collection_instances.collection` takes the SOURCE path** (`/hierarchy/sub1.collection` in Defold's own fixtures) — the deliberate asymmetry with item 10. `add_subcollection` stores the source path.

## Engine and test runs (`test_runner.py`)

12. **The engine prefixes `print()` with `LEVEL:DOMAIN: `** (`DEBUG:SCRIPT: TEST {…}`) — the TEST-stream parser strips exactly one prefix before matching (also fixing the DefTest fallback).
13. **A subcollection is flattened into its parent and never archived** — `--config=bootstrap.main_collection=<subcollection>` is structurally impossible (engine exits 1 silently). `build_test_collection` writes a `[bootstrap] main_collection` settings file and runs `bob build --settings <file>` so the test collection IS the archive bootstrap.
14. **bob treats the settings file's directory as a content root** — the settings file must live in a private `tempfile.mkdtemp()` (removed in `finally`), never in the project (relative-path conflict) or plain `/tmp`.
15. **bob's log4j `SEVERE  <message>` lines are hard failures** — now parsed as error diagnostics (resource/line `None`), and compile/bundle failure responses carry a 2,000-char `stdout_tail` so agents can self-correct.

## CI live smoke (`.github/workflows/ci.yml`)

16. **`dmengine_headless` links X11 and OpenAL** — "headless" means it opens no window, not that it is library-free: `ldd` on the 1.14.1 x86_64-linux binary lists `libX11.so.6`, `libXext.so.6`, `libXi.so.6`, `libxcb.so.1`, `libXau.so.6`, `libXdmcp.so.6` and `libopenal.so.1`. A desktop host already carries them, which is why the smoke passed locally; the first GitHub-hosted runner run (a server image) instead let the dynamic loader refuse the engine, so it exited non-zero before any `TEST` event and `defold_test_run` answered `{ok: False, run: None, passed: 0}`. The live job now installs `libx11-6 libxext6 libxi6 libxcb1 libxau6 libxdmcp6 libopenal1` before `mise run live`, and `tests/test_workflows.py::test_live_job_installs_the_engine_runtime_libraries` pins both the package list and the step order. The same gap bounds the published image: it is a minimal Chainguard build, so it serves the MCP server, `defold_compile`, `defold_bundle` and the docs tools but not `defold_test_run` or `defold_run` (`docs/INSTALL.md`, Troubleshooting).

## Operational facts

- The artifact cache (`~/.cache/defold-mcp`) holds ~428 MB: `jdk-25/`, `defold-1.14.1/` (bob.jar, dmengine_headless), `builtins.zip`, `ref-doc.zip`, `docs.db` (≈4,100 docs: 2,142 lua_api, 1,145 cpp_api, 140 manuals, 140 examples, 262 assets), `resource-cache/`, `versions.json`.
- Gates: `mise run test` → 294 unit tests (live deselected); `mise run live` → the smoke in ~10 s warm. Never run them concurrently (the uv venv rebuild races).
- The wasm-web bundle layout: `build/wasm-web/<Title>/index.html`, `<Title>.wasm` (+ `<Title>_pthread.wasm`), `game.arcd`.
- The engine's idiom for ending a test run: `msg.post("@system:", "exit", { code = 0 })` — live-verified (exit 0).
