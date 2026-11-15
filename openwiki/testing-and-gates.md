---
type: testing
title: Testing and gates
description: How defold-mcp is tested — the mise setup/test/live task chain, the pytest live-marker separation, the asyncio fixture loop pin, and what the current 294-test suite plus the live gate covers thematically.
tags: [defold, testing, pytest, mise, gates]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T14:41:05.547Z
sources:
  - id: openwiki-source-72b5d686f860ea86c8592080
    resource: repo://mise.toml
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-e200d53fe0347c46249510fb
    resource: repo://tests/test_bob_diagnostics.py
  - id: openwiki-source-aa918e62d579257ce43a1ddf
    resource: repo://tests/test_deps_sync.py
  - id: openwiki-source-4100c25666ae22e213937002
    resource: repo://tests/test_docs_fts.py
  - id: openwiki-source-ec5f3515eba7d123ca68e770
    resource: repo://tests/test_mcp_stdio_e2e.py
  - id: openwiki-source-d0ba0764c79275c3c198800b
    resource: repo://tests/test_project_ini.py
  - id: openwiki-source-c7f35c27a3e804b5af2c8277
    resource: repo://tests/test_textproto_roundtrip.py
generated: { by: "opencode", at: "2026-11-15T14:41:05.547Z" }
---

# Testing and gates

## The mise task chain

`mise.toml` pins the toolchain (Python 3.14.10, uv 0.13.7) and defines the task chain used as the gate:

- **`mise run setup`** — recreates the virtual environment (`uv venv .venv --python 3.14 --clear`) and installs the package editable with dev extras (`uv pip install -e ".[dev]"`). `--clear` makes it idempotent, so downstream tasks can depend on it safely.
- **`mise run test`** — the default gate: `.venv/bin/pytest -m "not live" -q`. It depends on `setup`, so a clean checkout needs no manual bootstrap.
- **`mise run live`** — the complementary gate for tests marked `live` (real Defold artifact downloads and minutes-long builds, yet to be added): `pytest -m live -v -s`.
- **`mise run handshake`** — reserved for the stdio MCP handshake probe script (not yet written); depends on `setup`.

The gates are intentionally split: unit tests stay fast and hermetic (no network, no JVM), while live integration tests are opt-in via the marker so CI and quick loops are never blocked on multi-minute Defold builds.

## pytest configuration

`[tool.pytest.ini_options]` in `pyproject.toml` registers the `live` marker (with a description telling how to deselect it) and pins `asyncio_default_fixture_loop_scope = "function"`. The pin silences the pytest-asyncio deprecation warning that otherwise fires on every run even with zero async tests, and fixes the fixture loop scope before async tests arrive in later phases.

## What the current suite covers

294 tests plus one live smoke, thematically:

- **Config (6, in `tests/test_config.py`):** cache-dir resolution; the exact six-entry ordered `COMPILED_SETTING_KEYS` tuple; the hard constants (Java 25, 1024-byte POST ceiling, default engine port); host-platform shape (skipif-guarded) and the unsupported-pair `ValueError` message.
- **game.project editor (18, in `tests/test_project_ini.py`):** root resolution (walk, env var, eight-parent cap); compiled-path `c` suffix idempotency; indexed and legacy dependency forms and gap-free rewrites; `include_dirs` splitting; line preservation, CRLF byte-fidelity, missing-section creation, blank-line-aware insertion; values containing `=`; `get()` defaults; `delete`/`delete_section`; injection guards.
- **Textproto engine (46, in `tests/test_textproto_roundtrip.py`):** parser behaviour (scalar forms, adjacent-literal concatenation, double-escaped `.collection` decoding, always-list schema, exact uint64, error line numbers, colon-brace form, `max_depth`, hex/octal escapes); byte-exact round-trips over the three verified fixtures (scalar, `.go`, `.collection` with the inner closing-quote pin); `BareIdent` quoting both directions; math default omission; mutator guardrails; collision shapes (opaque-bounds box, CCW hulls, empty/collinear rejections).
- **Toolchain + deps (22, in `tests/test_deps_sync.py`):** version resolution (TTL cache with request counting, channel non-200 raises, pinned-404 fallback, malformed-200 raises, nothing cached on failure); `check_java` (fake Java 21/25/missing, portable-first, broken-portable fallback to a working JAVA_HOME); `download_artifact` magic checks and atomicity; `install_jdk25` idempotence and extraction hardening (`..` tar escape end-to-end, backslash traversal, multi-root zip, layout check); `extract_zip` zip-slip in both forms (`../` and absolute entries); **the sandbox-escape regression** (dependency URLs ending `/..` and `/.` extract under hash names, project root untouched); end-to-end `.deps` sync over `httpx.MockTransport` with in-memory zips; ignore-file repair.
- **Docs FTS index (14, in `tests/test_docs_fts.py`):** the pinned FTS5 schema and porter stemming (behavioural, bidirectional); manual/example ingestion; the **real-format regressions** (ref-doc namespace-document shape ranking `go.animate` #1; the `{"items": […]}` asset feed with strict rejection of other shapes); asset stars ranking with `unlisted`/`scene3d` excluded from the DB entirely; hostile FTS query handling; `get_doc` 25k cap, pagination and clamps; the codeload tarball ingestion with scope classification and `..`-slug skipping; re-ingestion idempotency.
- **Editor client (25, in `tests/test_editor_client.py`):** probe behaviour (missing port, refused connection, malformed/out-of-range ports, 200 binding); compile 200/422 with 0-to-1-based conversion and malformed-body degradation; all six command routes and the build-html5 URL; preview percent-encoding (wire-form `raw_path` assertions), PNG magic, atomic writes, 404/422 mapping; console and `/bob` bearer-body round-trips; `/eval` retry semantics (503 backoff sequences monkeypatched — no wall-clock; 422 exactly-once), non-JSON tolerance; `lua_quote` round-trips over adversarial strings and script-safety assertions; `transact_mutations` op generation; **token hygiene** (absent from messages, bodies, `repr` and the exception chain — `from None` chain scrubbing pinned).
- **Runners (21, in `tests/test_bob_diagnostics.py`):** bob diagnostics (both prefixes, JVM noise filtering, missing-line case), build-report top-10/total/shapeless rejection, missing-report degradation with warning; `run_bob` argv construction and cwd via fake `java` executables that write their argv to files, the java-missing error path, timeout kill with partial output and **no orphaned process**; test-stream parsing (TEST events, run-id gating with runless rejection, per-test statuses, suite totals, DefTest fallback only without TEST events), `--config` override rendering (booleans lowercase, multi-flag), `.collectionc` idempotence, `game.project` byte-identity across runs, engine timeout kill.
- **Engine service client (25, in `tests/test_engine_protobuf.py`):** the exact wire-byte vectors (reload, system int32/string messages, two-byte varints); the chunker boundary with reassembly and order checks; a real local asyncio TCP server driving handshake verification, the 5,000-line ring, **eviction-safe pagination** (the two-read disjointness invariant) and filter semantics; the mocked recording protocol (headers on both calls, stop-500 aborts before polling, receipt verification, orphaned file_id, DELETE-exactly-once).
- **Automation bridge (16, in `tests/test_bridge_v2.py`):** the DLIB 500 unwrapping (structured 409/stale_scene), raw-500 degradation, 200-with-ok-false tolerance; inspection routes with both element shapes; lease acquire/no-re-PUT/renewal-same-UUID; the full click flow with guards; automation_id not-found; poll timeout with last status; screenshot success, sha mismatch and the verified-flag honesty check.
- **Templates (21, in `tests/test_templates.py`):** all four profiles render with per-profile physics/engine/HTML5 keys; the rendered main.collection round-trips byte-exactly through parse→serialize; the embedded sprite (2D) / camera (3d_pbr) trees; the licence notice module mounted and printed by all four profiles; Lua-escaped titles; dependency URL wiring; unknown-profile rejection.
- **Mutations registry Part C (29, in `tests/test_textproto_roundtrip.py`):** one test per registry op (create, add_instance, add_embedded_instance, add_subcollection, add_component, set_property, remove_child, atlas ops, tilesource/tilemap, gui ops, collision_add_shape both variants) plus guard rejections (multiline strings, missing types), unknown-op indexed errors, and the end-to-end create→add_component→add_instance→serialize→re-parse byte-exact chain; TestSchemaOrderedSerialisation pins the extension= canonical ordering and the default's byte-exactness.
- **Stdio e2e (14, in `tests/test_mcp_stdio_e2e.py`):** drives the REAL defold-mcp binary over newline-delimited JSON-RPC — the handshake and exact-20-tools list in order; the project_init → info → mutate → inspect workflow over real files with byte-exact assertions; **traversal containment over the live server** (`..`, normalised, symlink escapes rejected; inside-root allowed); the 25k cap on escaping-heavy payloads; payload-dialect editor routing with a stubbed probe; double-run orphan prevention; filtered process-log paging (disjoint, monotone, eviction-safe); backend-key honesty; stdout purity (every frame parses as JSON); clean SIGTERM.

**Live-phase suite additions** (the gate moved 265 → 294, all hermetic): channel-matched pinned-spec resolution with the stable-over-beta preference lock; redirect-following clients; JDK exec-bit preservation and executable-aware re-install; artifact-cache reuse with magic re-verification (corrupt engine re-fetch); docs-db self-heal (missing/malformed/empty); the SEVERE diagnostics and compile/bundle `stdout_tail` on failure; the `--settings` test-build route (private temp dir, failure cleanup); the `LEVEL:DOMAIN:` log-prefix stripping; source-path `add_subcollection`; template `game.project` bob-parsing guards (no comment lines, required `input.game_binding`, c-suffixed values).

- **The live smoke (`@pytest.mark.live test_live_defold_1_14_1_build_and_headless_test`, in `tests/test_mcp_stdio_e2e.py`):** drives the REAL server over stdio against the REAL toolchain — `defold_setup_toolchain("1.14.1", install_jdk25=True)` with all artifacts asserted on disk, `defold_docs_search("go.animate")` returning the real signature, a scaffolded project + test collection compiled by real bob under JDK 25, a real headless `dmengine_headless` run (`run="smoke1"`, passed 2 / failed 0, exit 0) and a real wasm-web bundle (index.html + non-empty .wasm + non-empty game.arcd). Excluded from the default gate; `mise run live` runs it alone.

## Conventions

- Plain pytest functions for the foundation suites; grouped classes for the textproto Part B groups — both styles accepted, grouping preferred when one fixture serves many small cases.
- Byte-level assertions wherever newline translation could mask a bug; fixture bytes for round-trip tests are the verified Defold 1.14.1 output, asserted with exact string equality.
- All HTTP through `httpx.MockTransport`; all archives in-memory (`zipfile`/`tarfile` over `BytesIO`); all filesystem state in `tmp_path` or monkeypatched `DEFOLD_MCP_CACHE_DIR`.
- `pytest.raises(..., match=...)` for exception-contract tests, asserting distinguishing substrings rather than whole messages.
- Strict red → green TDD, with the gate re-run (`mise run test`) after every task.
