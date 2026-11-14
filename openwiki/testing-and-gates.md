---
type: testing
title: Testing and gates
description: How defold-mcp is tested — the mise setup/test/live task chain, the pytest live-marker separation, the asyncio fixture loop pin, and what the current 24-test suite covers thematically.
tags: [defold, testing, pytest, mise, gates]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-14T16:49:47.288Z
sources:
  - id: openwiki-source-72b5d686f860ea86c8592080
    resource: repo://mise.toml
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-81af13fa7982f0b3becf1286
    resource: repo://tests/test_config.py
  - id: openwiki-source-d0ba0764c79275c3c198800b
    resource: repo://tests/test_project_ini.py
generated: { by: "opencode", at: "2026-11-14T16:49:47.288Z" }
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

`[tool.pytest.ini_options]` in `pyproject.toml` registers the `live` marker (with a description telling how to deselect it) and pins `asyncio_default_fixture_loop_scope = "function"`. The pin silences the pytest-asyncio deprecation warning that otherwise fires on every run even with zero async tests, and fixes the fixture loop scope before async tests arrive in later phases. Failing to register the marker would make `-m` selection raise an error under strict-marker behaviour, so the marker description doubles as documentation.

## What the current suite covers

24 tests, thematically:

- **Config (6, in `tests/test_config.py`):** cache-dir resolution via the `DEFOLD_MCP_CACHE_DIR` override and the `HOME`-based default; the exact six-entry ordered `COMPILED_SETTING_KEYS` tuple asserted with `==` (order-sensitive, no extras); the three hard constants (Java 25, 1024-byte POST ceiling, default engine port 8001); the host-platform shape for x86_64-Linux hosts (guarded with `skipif` so the suite stays portable); and the `ValueError` path for unsupported platform/machine pairs, monkeypatching `platform.system`/`platform.machine` and asserting the message names the offending pair and the supported combinations.
- **game.project editor (18, in `tests/test_project_ini.py`):** project-root resolution (upward walk, env var, and the eight-parent cap raising `FileNotFoundError`); the compiled-path `c` suffix and its idempotency; dependencies in both modern indexed (`dependencies#N`, numeric ordering, indexed-beats-legacy precedence) and legacy comma-separated forms, plus the rewrite to gap-free sequential keys on add/remove (including legacy-only files and end-of-file section insertion); `include_dirs` splitting on mixed separators; line preservation on save (comments, blank lines, trailing newline, missing-section creation, blank-line-aware insertion position); CRLF byte-fidelity (fixture written with `write_bytes`, assertion reads bytes back); values containing `=` round-tripping; `get()` defaults; `delete`/`delete_section`; and the mutator injection guards rejecting newline/CR and `]` in sections without touching file state.

## Conventions

- Plain pytest functions, `tmp_path` and `monkeypatch` fixtures, no test classes.
- Byte-level assertions (`read_bytes`) wherever newline translation could mask a bug.
- `pytest.raises(..., match=...)` for exception-contract tests, asserting distinguishing substrings rather than whole messages.
- One suite-level rule from the repo's wider process: strict red → green TDD, with the gate re-run (`mise run test`) after every task.
