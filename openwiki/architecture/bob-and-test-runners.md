---
type: architecture
title: Bob and headless test runners
description: How defold-mcp runs bob.jar builds (java resolution, diagnostics parsing, build reports) and dmengine_headless test collections (--config overrides, TEST event parsing, guaranteed timeout kills).
tags: [defold, bob, java, testing, subprocess]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T14:41:05.547Z
sources:
  - id: openwiki-source-59b94445da06839fcf0abbbe
    resource: repo://src/defold_mcp/bob_runner.py
  - id: openwiki-source-60018a68c712ae4c069cbdcc
    resource: repo://src/defold_mcp/test_runner.py
  - id: openwiki-source-e200d53fe0347c46249510fb
    resource: repo://tests/test_bob_diagnostics.py
generated: { by: "opencode", at: "2026-11-15T14:41:05.547Z" }
---

# Bob and headless test runners

`src/defold_mcp/bob_runner.py` and `src/defold_mcp/test_runner.py` are the headless execution pair: one drives Defold's `bob.jar` for builds and bundles, the other drives `dmengine_headless` for deterministic test runs. Both wrap `subprocess` with a hard rule learned from the CPython source: **on timeout the child is killed and waited before the exception raises** (`subprocess.run` semantics, verified against the installed 3.14.10 source), and — the subtle part — `TimeoutExpired.stdout` carries **raw bytes** on the timeout path (text decoding happens only on the success path), so both modules normalise through a `_to_text` helper before attaching partial output to their timeout errors.

## `bob_runner.py`

- **`run_bob(project_root, commands, …)`** resolves Java as: explicit override → `toolchain.java_executable()` → `RuntimeError` naming the JDK 25 requirement and `defold_setup_toolchain(install_jdk25=True)`. The argv is `<java> -jar <bob.jar> <commands…> --resource-cache-local <cache>/resource-cache` plus `--platform`/`--variant`/`--archive`/`--build-report-json`/`--bundle-format`/`--architectures` as given, always run with `cwd=project_root` and a combined stdout+stderr capture (bob prints diagnostics on stdout; JVM noise lands on stderr).
- **`parse_bob_diagnostics(output)`** extracts `{severity, resource, line, message}` — tolerant of `ERROR `/`ERROR: ` prefixes and bob's log4j `SEVERE  <message>` hard-failure lines (severity "error", resource/line `None`), 1-based integer lines (`None` when absent), and silently ignores the JDK 25 restricted-method warning block. The `settings=` kwarg threads a bob `--settings <file>` override (used by the test runner's build step).
- **`summarize_build_report(report)`** loads Bob's `--build-report-json` output (str/bytes/Path), locates the resources list, and returns the total size plus the top-10 largest resources descending; a shapeless report raises `ValueError`. When bob exits 0 but the report file is missing, the runner degrades to `build_report=None` plus a warning diagnostic rather than discarding the successful `BobResult`.
- **`BobTimeoutError`** carries the partial output; `BobResult` reports `ok = exit 0 and no ERROR diagnostics` (warnings keep ok True).

## `test_runner.py`

- **`build_test_collection(project_root, test_collection, …)`** — the LIVE-phase addition, and the reason the plan's original route was retired: subcollections are FLATTENED into their parents and never archived, so the engine can never boot `/test/x.collectionc` via `--config`. The runner instead writes a minimal `[bootstrap] main_collection = <compiled>` settings file into a PRIVATE `tempfile.mkdtemp()` (bob treats the settings file's directory as a content root — a settings file inside the project causes a relative-path conflict) and runs `run_bob(root, ["build"], settings=<file>)`, removing the directory in a `finally`. On failure the tool reports a structured build error instead of letting the engine exit silently.
- **`run_headless_tests(project_root, test_collection, …)`** resolves the engine (override → `toolchain.ensure_toolchain(spec)`'s `dmengine_headless`), enforces the compiled collection form (`.collection` → `.collectionc`, idempotent) and runs `<engine> --config=bootstrap.main_collection=/test/x.collectionc [--config overrides, booleans lowercase] <project_root>`. **`game.project` on disk is never mutated.** The engine routes `print()` through its logger, so every `TEST {json}` and DefTest line arrives prefixed `LEVEL:DOMAIN: ` (e.g. `DEBUG:SCRIPT: `) — the parser strips exactly one prefix before matching. The tool response carries `run`, `status`, `exit_code` and `rejected_events` alongside the counts.
- **`parse_test_stream(output, expected_run=…)`** parses `TEST {json}` events: the first event carrying a run id fixes the run; events from a different run — or runless events once the run is fixed — are counted under `rejected_events` and never folded into the summary. `test_start`/`test_end` events build the per-test list; `suite_end` carries the totals. DefTest text summaries (`Success: X passed, Y failed` / `Failure: …`) are a fallback used **only** when no TEST events appear. The result carries a 2,000-char `stdout_tail`.
- **`TestTimeoutError`** carries the partial output; the engine is dead before it raises.

## Where the behaviour is pinned

`tests/test_bob_diagnostics.py` (21 tests) uses the fake-executable pattern — small POSIX shell scripts standing in for `java` and `dmengine_headless` (writing their argv to files, echoing diagnostic/TEST lines, or sleeping past the timeout) — so the whole suite runs hermetically: no JVM, no engine, no network, and the timeout-kill claims are asserted against real processes (`pgrep`-checked).
