---
type: testing
title: Testing and gates
description: How defold-mcp is tested — the mise setup/test/live task chain, the pytest live-marker separation, the asyncio fixture loop pin, and what the current 183-test suite covers thematically.
tags: [defold, testing, pytest, mise, gates]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T04:08:33.115Z
sources:
  - id: openwiki-source-72b5d686f860ea86c8592080
    resource: repo://mise.toml
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-e200d53fe0347c46249510fb
    resource: repo://tests/test_bob_diagnostics.py
  - id: openwiki-source-3ff7724e8a892710545e18d3
    resource: repo://tests/test_bridge_v2.py
  - id: openwiki-source-aa918e62d579257ce43a1ddf
    resource: repo://tests/test_deps_sync.py
  - id: openwiki-source-4100c25666ae22e213937002
    resource: repo://tests/test_docs_fts.py
  - id: openwiki-source-b70833c89ec665a468448695
    resource: repo://tests/test_engine_protobuf.py
  - id: openwiki-source-d0ba0764c79275c3c198800b
    resource: repo://tests/test_project_ini.py
  - id: openwiki-source-c7f35c27a3e804b5af2c8277
    resource: repo://tests/test_textproto_roundtrip.py
generated: { by: "opencode", at: "2026-11-15T04:08:33.115Z" }
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

183 tests, thematically:

- **Config (6, in `tests/test_config.py`):** cache-dir resolution; the exact six-entry ordered `COMPILED_SETTING_KEYS` tuple; the hard constants (Java 25, 1024-byte POST ceiling, default engine port); host-platform shape (skipif-guarded) and the unsupported-pair `ValueError` message.
- **game.project editor (18, in `tests/test_project_ini.py`):** root resolution (walk, env var, eight-parent cap); compiled-path `c` suffix idempotency; indexed and legacy dependency forms and gap-free rewrites; `include_dirs` splitting; line preservation, CRLF byte-fidelity, missing-section creation, blank-line-aware insertion; values containing `=`; `get()` defaults; `delete`/`delete_section`; injection guards.
- **Textproto engine (46, in `tests/test_textproto_roundtrip.py`):** parser behaviour (scalar forms, adjacent-literal concatenation, double-escaped `.collection` decoding, always-list schema, exact uint64, error line numbers, colon-brace form, `max_depth`, hex/octal escapes); byte-exact round-trips over the three verified fixtures (scalar, `.go`, `.collection` with the inner closing-quote pin); `BareIdent` quoting both directions; math default omission; mutator guardrails; collision shapes (opaque-bounds box, CCW hulls, empty/collinear rejections).
- **Toolchain + deps (22, in `tests/test_deps_sync.py`):** version resolution (TTL cache with request counting, channel non-200 raises, pinned-404 fallback, malformed-200 raises, nothing cached on failure); `check_java` (fake Java 21/25/missing, portable-first, broken-portable fallback to a working JAVA_HOME); `download_artifact` magic checks and atomicity; `install_jdk25` idempotence and extraction hardening (`..` tar escape end-to-end, backslash traversal, multi-root zip, layout check); `extract_zip` zip-slip in both forms (`../` and absolute entries); **the sandbox-escape regression** (dependency URLs ending `/..` and `/.` extract under hash names, project root untouched); end-to-end `.deps` sync over `httpx.MockTransport` with in-memory zips; ignore-file repair.
- **Docs FTS index (14, in `tests/test_docs_fts.py`):** the pinned FTS5 schema and porter stemming (behavioural, bidirectional); manual/example ingestion; the **real-format regressions** (ref-doc namespace-document shape ranking `go.animate` #1; the `{"items": […]}` asset feed with strict rejection of other shapes); asset stars ranking with `unlisted`/`scene3d` excluded from the DB entirely; hostile FTS query handling; `get_doc` 25k cap, pagination and clamps; the codeload tarball ingestion with scope classification and `..`-slug skipping; re-ingestion idempotency.
- **Editor client (25, in `tests/test_editor_client.py`):** probe behaviour (missing port, refused connection, malformed/out-of-range ports, 200 binding); compile 200/422 with 0-to-1-based conversion and malformed-body degradation; all six command routes and the build-html5 URL; preview percent-encoding (wire-form `raw_path` assertions), PNG magic, atomic writes, 404/422 mapping; console and `/bob` bearer-body round-trips; `/eval` retry semantics (503 backoff sequences monkeypatched — no wall-clock; 422 exactly-once), non-JSON tolerance; `lua_quote` round-trips over adversarial strings and script-safety assertions; `transact_mutations` op generation; **token hygiene** (absent from messages, bodies, `repr` and the exception chain — `from None` chain scrubbing pinned).
- **Runners (21, in `tests/test_bob_diagnostics.py`):** bob diagnostics (both prefixes, JVM noise filtering, missing-line case), build-report top-10/total/shapeless rejection, missing-report degradation with warning; `run_bob` argv construction and cwd via fake `java` executables that write their argv to files, the java-missing error path, timeout kill with partial output and **no orphaned process**; test-stream parsing (TEST events, run-id gating with runless rejection, per-test statuses, suite totals, DefTest fallback only without TEST events), `--config` override rendering (booleans lowercase, multi-flag), `.collectionc` idempotence, `game.project` byte-identity across runs, engine timeout kill.
- **Engine service client (25, in `tests/test_engine_protobuf.py`):** the exact wire-byte vectors (reload, system int32/string messages, two-byte varints); the chunker boundary with reassembly and order checks; a real local asyncio TCP server driving handshake verification, the 5,000-line ring, **eviction-safe pagination** (the two-read disjointness invariant) and filter semantics; the mocked recording protocol (headers on both calls, stop-500 aborts before polling, receipt verification, orphaned file_id, DELETE-exactly-once).
- **Automation bridge (16, in `tests/test_bridge_v2.py`):** the DLIB 500 unwrapping (structured 409/stale_scene), raw-500 degradation, 200-with-ok-false tolerance; inspection routes with both element shapes; lease acquire/no-re-PUT/renewal-same-UUID; the full click flow with guards; automation_id not-found; poll timeout with last status; screenshot success, sha mismatch and the verified-flag honesty check.

## Conventions

- Plain pytest functions for the foundation suites; grouped classes for the textproto Part B groups — both styles accepted, grouping preferred when one fixture serves many small cases.
- Byte-level assertions wherever newline translation could mask a bug; fixture bytes for round-trip tests are the verified Defold 1.14.1 output, asserted with exact string equality.
- All HTTP through `httpx.MockTransport`; all archives in-memory (`zipfile`/`tarfile` over `BytesIO`); all filesystem state in `tmp_path` or monkeypatched `DEFOLD_MCP_CACHE_DIR`.
- `pytest.raises(..., match=...)` for exception-contract tests, asserting distinguishing substrings rather than whole messages.
- Strict red → green TDD, with the gate re-run (`mise run test`) after every task.
