---
type: architecture
title: Bob and headless test runners
description: How defold-mcp runs bob.jar builds (java resolution, diagnostics parsing, build reports) and dmengine_headless test collections (--config overrides, TEST event parsing, guaranteed timeout kills).
tags: [defold, bob, java, testing, subprocess]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T02:39:02.076Z
sources:
  - id: openwiki-source-59b94445da06839fcf0abbbe
    resource: repo://src/defold_mcp/bob_runner.py
  - id: openwiki-source-60018a68c712ae4c069cbdcc
    resource: repo://src/defold_mcp/test_runner.py
  - id: openwiki-source-e200d53fe0347c46249510fb
    resource: repo://tests/test_bob_diagnostics.py
generated: { by: "opencode", at: "2026-11-15T02:39:02.076Z" }
---

# Bob and headless test runners

`src/defold_mcp/bob_runner.py` and `src/defold_mcp/test_runner.py` are the headless execution pair: one drives Defold's `bob.jar` for builds and bundles, the other drives `dmengine_headless` for deterministic test runs. Both wrap `subprocess` with a hard rule learned from the CPython source: **on timeout the child is killed and waited before the exception raises** (`subprocess.run` semantics, verified against the installed 3.14.10 source), and — the subtle part — `TimeoutExpired.stdout` carries **raw bytes** on the timeout path (text decoding happens only on the success path), so both modules normalise through a `_to_text` helper before attaching partial output to their timeout errors.

## `bob_runner.py`

- **`run_bob(project_root, commands, …)`** resolves Java as: explicit override → `toolchain.java_executable()` → `RuntimeError` naming the JDK 25 requirement and `defold_setup_toolchain(install_jdk25=True)`. The argv is `<java> -jar <bob.jar> <commands…> --resource-cache-local <cache>/resource-cache` plus `--platform`/`--variant`/`--archive`/`--build-report-json`/`--bundle-format`/`--architectures` as given, always run with `cwd=project_root` and a combined stdout+stderr capture (bob prints diagnostics on stdout; JVM noise lands on stderr).
- **`parse_bob_diagnostics(output)`** extracts `{severity, resource, line, message}` — tolerant of both `ERROR ` and `ERROR: ` prefixes, 1-based integer lines (`None` when absent), and silently ignores the JDK 25 restricted-method warning block (`WARNING: A restricted method in java.lang.System has been called`, its `Consider enabling…` follow-up and `java.lang.instrument` warnings).
- **`summarize_build_report(report)`** loads Bob's `--build-report-json` output (str/bytes/Path), locates the resources list, and returns the total size plus the top-10 largest resources descending; a shapeless report raises `ValueError`. When bob exits 0 but the report file is missing, the runner degrades to `build_report=None` plus a warning diagnostic rather than discarding the successful `BobResult`.
- **`BobTimeoutError`** carries the partial output; `BobResult` reports `ok = exit 0 and no ERROR diagnostics` (warnings keep ok True).

## `test_runner.py`

- **`run_headless_tests(project_root, test_collection, …)`** resolves the engine (override → `toolchain.ensure_toolchain(spec)`'s `dmengine_headless`), enforces the compiled collection form (`.collection` → `.collectionc`, idempotent) and builds `<engine> --config=bootstrap.main_collection=/test/x.collectionc [--config=k=v for each config override — booleans lowercase] <project_root>`. **`game.project` on disk is never mutated** — overrides travel only as `--config` flags (pinned by a byte-comparison test). The plan's alternative route (a temporary `--settings` file for bob) is deferred to the wiring task and documented.
- **`parse_test_stream(output, expected_run=…)`** parses `TEST {json}` events: the first event carrying a run id fixes the run; events from a different run — or runless events once the run is fixed — are counted under `rejected_events` and never folded into the summary. `test_start`/`test_end` events build the per-test list; `suite_end` carries the totals. DefTest text summaries (`Success: X passed, Y failed` / `Failure: …`) are a fallback used **only** when no TEST events appear. The result carries a 2,000-char `stdout_tail`.
- **`TestTimeoutError`** carries the partial output; the engine is dead before it raises.

## Where the behaviour is pinned

`tests/test_bob_diagnostics.py` (21 tests) uses the fake-executable pattern — small POSIX shell scripts standing in for `java` and `dmengine_headless` (writing their argv to files, echoing diagnostic/TEST lines, or sleeping past the timeout) — so the whole suite runs hermetically: no JVM, no engine, no network, and the timeout-kill claims are asserted against real processes (`pgrep`-checked).
