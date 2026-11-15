---
name: defold
description: Use when building, testing, or bundling Defold games.
version: 0.1.0
license: MIT
---

# Defold

Defold is a data-oriented 2D/3D game engine that produces every target — HTML5,
Linux, Windows, macOS, Android and iOS — from a single project with no IDE in the
build path. This skill drives Defold 1.14+ through the `defold_*` MCP tools, which
work in two interchangeable modes: **Live Editor** (a Defold Editor is open, so
writes land as transactions that preserve unsaved tabs and undo history) and
**Headless** (`bob.jar` plus `dmengine_headless`, for CI and remote sessions).
Every project-scoped response names the mode that served it: `"backend": "editor"`
or `"backend": "headless"`.

## When to Use

Use this skill for any Defold game project: 2D or 3D, any genre, any of Defold's
bundle targets. It covers scaffolding, structured resource authoring, deterministic
compilation and tests, runtime inspection, screenshots and bundling.

**Don't use for:** non-Defold engines (Unity, Godot, Unreal, Love2D, GameMaker), and
raw engine C++ work inside the Defold source tree itself — this skill never
modifies the engine.

## Prerequisites

1. The `defold` MCP server is registered with the harness (see `docs/INSTALL.md`);
   restart the client or start a new session after registering.
2. Orient before writing anything: call `defold_doctor`. It reports the JDK, the
   cached Defold artifacts, the detected backend, `.deps/` freshness and the state
   of the documentation index.
3. Provision on a cold cache: call `defold_setup_toolchain`. It downloads the
   pinned Defold 1.14.1 artifacts (`bob.jar`, `dmengine`, `dmengine_headless`,
   builtins, `ref-doc.zip`), builds the offline documentation index, and installs a
   private Temurin JDK 25 under `~/.cache/defold-mcp/jdk-25` when the host JDK is
   older — `bob.jar` for 1.14.x requires class-file version 69, so JDK 21 will not
   do. The cache is roughly 428 MB and is reused across projects.
4. Let the first call be patient: `defold_setup_toolchain` legitimately takes
   minutes. Clients must allow at least 600 s (900 s preferred) per tool call.

## Quick Reference

Tools are advertised with bare names. Harnesses prefix them on ingest
(`mcp__defold__defold_doctor`, `defold__defold_compile`, `mcp_defold_defold_run`);
always refer to the bare name and let the harness resolve its own prefix.

| Tool | Use it for |
|---|---|
| **Orient** | |
| `defold_doctor` | Host and project health: JDK, cache, backend, `.deps/`, docs index. |
| `defold_project_info` | `game.project` summary, bootstrap paths, display/physics settings, resource inventory. |
| **Set up and inspect** | |
| `defold_setup_toolchain` | Download and cache the pinned toolchain (and the JDK 25 if needed). |
| `defold_project_init` | Scaffold a project from a verified profile: `2d_pixel`, `2d_hd`, `3d_pbr`, `minimal`. |
| `defold_sync_deps` | `action="list" \| "add" \| "remove" \| "sync"` dependencies; `sync` unpacks `.deps/` and builtins. |
| `defold_list_resources` | List resources by extension or glob, optionally including `.deps/`. |
| `defold_inspect_resource` | Parse a resource to clean JSON, or outline a Lua script's properties and callbacks. |
| **Documentation and assets** | |
| `defold_docs_search` | Offline FTS5 search over manuals, Lua/C++/extension APIs, examples and the Asset Portal. |
| **Mutate** | |
| `defold_project_settings` | `get`/`set`/`delete` one `game.project` setting, preserving comments and the compiled-path `c` suffix. |
| `defold_mutate_resource` | Batch structured edits to `.collection`, `.go`, `.gui`, `.atlas`, `.tilesource`, `.tilemap`, `.collisionobject`, `.sprite`, `.factory`, `.material`. |
| **Verify** | |
| `defold_compile` | `bob` (or Editor) build; returns `ok` with diagnostics carrying resource, line and column. |
| `defold_test_run` | Build a test collection as the entry point, run it headless, parse `TEST {...}` events. |
| **See it** | |
| `defold_preview` | Static PNG of a `.collection`, `.go`, `.gui`, `.model`, `.atlas` or `.tilemap` (Editor only). |
| `defold_run` | Launch the game: Editor run, or a managed `dmengine` process. |
| `defold_runtime_act` | `screenshot`, `click`, `drag`, `drag_path`, `key`, `wheel`, `record_start`/`record_stop`, `system_command`. |
| **Deliver** | |
| `defold_bundle` | `wasm-web`, `x86_64-linux`, `arm64-linux`, `x86_64-win32`, `arm64-macos`, `x86_64-macos`, `armv7-android`, `arm64-ios`. |
| **Run control** | |
| `defold_stop` | Stop the running game cleanly and return the trailing log lines. |
| `defold_hot_reload` | Hot-swap modified scripts and images into a running debug engine. |
| `defold_logs` | Incremental logs from the Editor console, the spawned process ring buffer or the engine TCP stream. |
| `defold_runtime_inspect` | Live `info`, `state`, `scene`, `elements`, `element`, `screen`. |

## Procedure

Work the loop in order. Each step has a completion criterion; do not start the next
step until the current one is satisfied.

1. **Orient and sync.** `defold_doctor` → `defold_project_info`. Run
   `defold_sync_deps(action="sync")` whenever `.deps/` is missing or older than
   `game.project`, which is what makes `/builtins` and library paths greppable and
   resolvable. *Done when:* `backend` is reported, dependencies show no `missing`
   entries and the resource inventory matches the files on disk.
2. **Check the Asset Portal before reinventing anything.**
   `defold_docs_search(query="...", scope="assets")`. Prefer a maintained library
   over new code — Monarch for screens and transitions, Druid for GUI components,
   DefTest for test suites, DAABBCC for broad-phase 2D queries, Bullet3D for 3D
   physics. *Done when:* the search either names the library you will use or shows
   that nothing suitable exists.
3. **Build the scene structurally.** Use `defold_mutate_resource` with a batch of
   operations — `create`, `add_instance`, `add_embedded_instance`,
   `add_subcollection`, `add_component`, `set_property`, `remove_child`,
   `atlas_add_images`, `atlas_set_animation`, `tilesource_configure`,
   `tilemap_set_tiles`, `gui_add_node`, `gui_set_layout_property`,
   `collision_add_shape`. Never hand-edit escaped `data:` blocks in `.collection` or
   `.go` files. *Done when:* `defold_inspect_resource` returns the tree you intended
   and the file still round-trips byte-exactly.
4. **Implement behaviour with the harness's own file tools.** Write
   `.script`, `.gui_script`, `.render_script`, `.lua`, `.vp` and `.fp` files
   directly; those are plain text, and the MCP server does not need to own them.
   Honour every rule in **Pitfalls** below, and check existing APIs with
   `defold_docs_search(scope="lua_api")` rather than guessing a signature. *Done
   when:* `defold_compile` reports zero errors — a warning-free build is the goal,
   an error-free build is the gate.
5. **Verify logic deterministically.** `defold_test_run(test_collection="/test/x.collection")`.
   It builds the test collection itself (no manual `game.project` edit, no
   mutation of your bootstrap settings), runs `dmengine_headless`, and returns
   `passed`/`failed` counts with failure detail. *Done when:* the suite is green and
   `failed` is 0.
6. **Verify visually, then deliver.** `defold_preview` for a static view of a scene,
   or `defold_run` followed by `defold_runtime_act(action="screenshot")` for a live
   frame — then open the returned PNG with the harness's image-view capability and
   actually look at it. Finish with `defold_bundle(platform="wasm-web")` (or another
   target) and confirm the artifacts exist and are non-empty. *Done when:* the
   screenshot matches the intent and the bundle contains a non-empty `game.arcd`
   plus the platform entry point.

## Pitfalls

These are the ten failure modes that models trained on other engines hit in Defold.
Treat them as hard invariants.

1. **Strict Lua 5.1 everywhere.** Desktop and Android run LuaJIT, but HTML5 runs
   PUC Lua 5.1.4. Lua 5.2's `_ENV`, Lua 5.3's bitwise operators (`&`, `|`, `<<`,
   `//`) and `goto` work locally and break in the browser. Use Lua 5.1 syntax and
   the `bit.*` library (`bit.band`, `bit.bor`, `bit.lshift`).
2. **Collections are compile-time.** A collection is flattened into
   slash-prefixed ids (`/team_1/bean`) at build time and does not exist as an
   addressable object at runtime. `set_parent` re-parents the transform hierarchy
   only; a game object's URL is fixed from spawn to deletion.
3. **Relative strings versus hashed ids.** `go.set_position(pos, "my_obj")` works
   because the engine hashes the string against the current collection. A bare
   `hash("my_obj")` has no collection context and fails inside a sub-collection —
   pass `hash("/full/path")` or the relative string. Never rely on `tostring(hash)`
   in game logic: reverse lookup is stripped in release builds and prints
   `[unknown]`.
4. **Four isolated script contexts.** `.script` has `go.*` and component APIs but no
   `gui.*` or `render.*`; `.gui_script` has `gui.*` but no `go.*`, no
   `go.property()`, no `fixed_update` or `late_update`; `.render_script` has
   `render.*`/`graphics.*`/`camera.*`, its `update(self)` receives no `dt` and its
   `on_message` receives no `sender`; `.editor_script` runs Lua 5.2 in the Editor
   JVM, entirely outside the engine, and long-running `editor.*` calls cannot run
   at top level. Cross-context traffic goes through `msg.post`.
5. **`self` is userdata, and the Lua state is shared.** `pairs(self)`,
   `ipairs(self)` and `pprint(self)` fail. With the default
   `script.shared_state = 1`, one VM is shared by every script instance, so a
   top-level `local` is shared across all instances — keep per-instance state on
   `self`. Empty `update`/`fixed_update` callbacks still cost a C-to-Lua call per
   instance per frame, so delete the ones you do not use.
6. **`go.property()` values must be literals.** Bob parses them statically before
   Lua runs, so `go.property("hp", 50 + 50)` fails to compile. Use `100`, `true`,
   `"str"`, `hash("x")`, `msg.url()`, `vmath.vector3(1, 2, 3)` or
   `resource.atlas("/main/a.atlas")`.
7. **`require` uses dot paths.** `require("main.utils")`, never
   `require("/main/utils.lua")`. Hot-reloading a module that returns a table leaves
   existing callers holding the old table unless `package.loaded["main.utils"]` is
   cleared and the dependants reload too.
8. **The compiler is reachability-driven.** Only the graph reachable from
   `game.project` (`bootstrap.main_collection`, `bootstrap.render`, …) is built.
   Data files loaded with `sys.load_resource("/data/levels.json")` must be declared
   in `[project] custom_resources = /data`, and a test collection must be reachable
   — instance it into the bootstrap collection or point `defold_test_run` at it.
9. **Shaders are `#version 140`.** Use `in`/`out` instead of
   `attribute`/`varying`, write the fragment colour to `out vec4 out_fragColor`
   (never `gl_FragColor`), sample with `texture(...)` (never `texture2D(...)`), and
   wrap every non-sampler uniform in a named block (`uniform vs_uniforms`,
   `uniform fs_uniforms`). `mtx_normal` yields view-space normals.
10. **Physics needs a sane scale.** Box2D expects moving bodies of 0.1–10 metres;
    the default `physics.scale = 1.0` turns a 100 px sprite into a 100-metre
    skyscraper. For 2D pixel and HD games set `scale = 0.01`,
    `gravity_y = -1000.0`, `velocity_threshold = 100.0`, `use_fixed_timestep = 1`
    and `[engine] fixed_update_frequency = 60`. Each collection proxy is its own
    physics world, so objects in different proxies never collide.

Two further rules of the same weight:

- **Never hand-edit double-escaped `data:` strings.** Nested Protobuf Text inside
  a `.collection` needs `\\\"` for inner quotes except on the closing line, and one
  wrong backslash makes the Editor refuse the file. Send a structured operation to
  `defold_mutate_resource` and let it serialise.
- **Ship the Defold licence notice.** Clause 4(b) of the Defold License requires
  the notice in your game. `defold_project_init` writes
  `license/defold_notice.lua` and mounts it, so keep it reachable — do not delete
  the module or detach its script when pruning the template.

## Verification

Never claim a feature works until the matching proof exists. The ladder is:

1. `defold_compile` → `ok: true` and an empty diagnostics list.
2. `defold_test_run` → `failed: 0` and every intended test name present, for any
   logic you added.
3. `defold_preview` or `defold_runtime_act(action="screenshot")` → the returned PNG
   opened and inspected, for anything visible.
4. `defold_bundle` → the artifact list contains the platform entry point and a
   non-empty `game.arcd`, for anything you call delivered.

Deeper references, loaded by relative path when a task needs them:

- [Architecture and Lua rules](references/architecture-and-lua-rules.md) — URLs,
  script contexts, `self`, properties, library idioms, message limits.
- [Shaders and materials](references/shaders-and-materials.md) — a complete
  `#version 140` vertex and fragment pair, instancing, normals, glTF PBR hooks.
- [Cross-platform and genres](references/cross-platform-and-genres.md) — the
  platform matrix, 2D/3D presets, ads and web-portal extensions, bundle commands.
- [Proto schemas quick reference](references/proto-schemas-quickref.md) — exact
  `defold_mutate_resource` payloads, guardrails and `game.project` path rules.
