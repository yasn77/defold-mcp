"""Headless test runner using the ``dmengine_headless`` engine binary.

The engine is invoked as
``<dmengine_headless> --config=<key>=<value> [more --config=...] <project-directory>``.
To run a TEST collection instead of the bootstrap one, the config override
``bootstrap.main_collection=<path>`` is passed. ``game.project`` on disk is
NEVER mutated — overrides go exclusively through ``--config`` flags.

Deferred: the plan's alternative route (a temporary ``--settings`` file
handed to bob) is not implemented; the ``--config`` route is used only.

Test events
-----------
The engine prints ``TEST `` events on stdout: one JSON object per line
after the literal prefix ``TEST ``. The first event carrying a ``run``
id fixes the run id; events with a DIFFERENT run id, or with NO run id
once one is fixed (or differing from ``expected_run`` when given), are
rejected — counted under ``rejected_events`` and never folded into the
summary. ``test_end`` events carry per-test status;
``suite_end`` carries the totals. DefTest (the community test framework)
instead prints text summaries such as ``Success: 4 passed, 0 failed``;
those are used only when no ``TEST`` events appear at all (TEST events
take precedence).

Timeouts
--------
``subprocess.run(..., timeout=...)`` is used. Verified against the CPython
3.14 source: on a timeout it calls ``process.kill()`` (SIGKILL on POSIX)
and ``process.wait()`` *before* re-raising ``TimeoutExpired``, so the child
is dead before :class:`TestTimeoutError` propagates. The partial output is
already populated in the exception on POSIX — but as raw **bytes** even in
text mode (the select-based ``_communicate`` joins the pipe data with
``b''.join`` in ``_check_timeout``; text decoding only happens on the
success path), so it is decoded here with UTF-8/replace. No extra
signalling is needed.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from defold_mcp import toolchain

# One JSON object per line after the literal ``TEST `` prefix.
_TEST_EVENT_RE = re.compile(r"^TEST\s+(\{.*\})\s*$")

# DefTest text summaries (tolerant: matched on the stripped line).
_DEFT_RE = re.compile(r"^(Success|Failure): (\d+) passed, (\d+) failed")

_STDOUT_TAIL_CHARS = 2000


def _to_text(value: "str | bytes | None") -> str:
    """Normalise captured output to text (timeout paths yield raw bytes)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


class TestTimeoutError(RuntimeError):
    """Raised when a headless engine run exceeds its timeout.

    ``partial_output`` carries whatever the process printed before it was
    killed (the child is guaranteed dead before this is raised).
    """

    def __init__(self, message: str, *, partial_output: str = "") -> None:
        super().__init__(message)
        self.partial_output = partial_output


def _compiled_collection_path(test_collection: str) -> str:
    """Return the compiled (``.collectionc``) form of a collection path.

    A ``.collection`` argument gets a ``c`` appended; an already-compiled
    ``.collectionc`` path is returned unchanged (idempotent).
    """
    if test_collection.endswith(".collectionc"):
        return test_collection
    if test_collection.endswith(".collection"):
        return test_collection + "c"
    return test_collection


def parse_test_stream(
    output: str, *, expected_run: "str | None" = None
) -> dict:
    """Parse a whole headless test run's output.

    Returns ``{"run": <run id or None>, "passed": int, "failed": int,
    "status": "pass"|"fail"|None, "tests": [{name, status, error?}...],
    "rejected_events": int, "stdout_tail": <last 2,000 chars>}``.

    The first ``TEST`` event carrying a ``run`` id fixes the run id;
    later events with a different run id, or with no run id once one is
    fixed (or differing from ``expected_run``) are rejected and counted,
    never counted. Totals come from ``suite_end``; when there are
    ``test_end`` events but no ``suite_end`` totals, they are derived from
    the per-test statuses. DefTest text summaries are used only when no
    ``TEST`` events appear at all.
    """
    run: "str | None" = None
    tests: list[dict] = []
    rejected_events = 0
    has_test_events = False
    passed = 0
    failed = 0
    status: "str | None" = None
    suite_totals = False
    text_passed: "int | None" = None
    text_failed: "int | None" = None
    text_status: "str | None" = None

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        match = _TEST_EVENT_RE.match(line)
        if match:
            try:
                event = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            has_test_events = True
            event_run = event.get("run")
            if expected_run is not None and event_run != expected_run:
                rejected_events += 1
                continue
            if run is None:
                run = event_run
            elif event_run is None or event_run != run:
                rejected_events += 1
                continue

            event_type = event.get("event")
            if event_type == "test_end":
                entry = {"name": event.get("name"), "status": event.get("status")}
                error = event.get("error") or event.get("stack")
                if error:
                    entry["error"] = error
                tests.append(entry)
            elif event_type == "suite_end":
                if isinstance(event.get("passed"), int):
                    passed = event["passed"]
                    suite_totals = True
                if isinstance(event.get("failed"), int):
                    failed = event["failed"]
                    suite_totals = True
                if event.get("status") in ("pass", "fail"):
                    status = event["status"]
            continue

        deft = _DEFT_RE.match(line.strip())
        if deft:
            text_passed = int(deft.group(2))
            text_failed = int(deft.group(3))
            text_status = "pass" if deft.group(1) == "Success" else "fail"

    if has_test_events:
        # TEST events take precedence over any DefTest text summary.
        if not suite_totals and tests:
            passed = sum(1 for t in tests if t["status"] == "pass")
            failed = sum(1 for t in tests if t["status"] == "fail")
            status = "fail" if failed else "pass"
    elif text_passed is not None:
        passed = text_passed
        failed = text_failed
        status = text_status

    return {
        "run": run,
        "passed": passed,
        "failed": failed,
        "status": status,
        "tests": tests,
        "rejected_events": rejected_events,
        "stdout_tail": output[-_STDOUT_TAIL_CHARS:],
    }


def _resolve_engine(engine: "str | Path | None", spec: str, client: Any) -> str:
    if engine is not None:
        return str(engine)
    result = toolchain.ensure_toolchain(spec, client=client)
    path = result.get("dmengine_headless")
    if not path or not Path(path).is_file():
        raise FileNotFoundError(
            f"dmengine_headless not found for spec {spec!r}; "
            "run defold_setup_toolchain(spec=...) first"
        )
    return str(path)


def run_headless_tests(
    project_root: "str | Path",
    test_collection: str,
    *,
    engine: "str | Path | None" = None,
    spec: str = "stable",
    timeout_s: float = 60,
    config_overrides: "dict | None" = None,
    client: Any = None,
) -> dict:
    """Run a test collection headlessly and return a parsed summary.

    The engine is resolved from the ``engine`` override or the spec's
    toolchain (``FileNotFoundError`` naming the spec when missing). The
    collection path is converted to its compiled ``.collectionc`` form and
    passed via ``--config=bootstrap.main_collection=...``; ``game.project``
    is never modified. Extra ``config_overrides`` become further
    ``--config=k=v`` flags.

    Returns the :func:`parse_test_stream` summary plus ``"ok"`` (exit code
    0 and no failures) and ``"exit_code"``. A timeout raises
    :class:`TestTimeoutError` with the partial output.
    """
    argv = [
        _resolve_engine(engine, spec, client),
        f"--config=bootstrap.main_collection={_compiled_collection_path(test_collection)}",
    ]
    for key, value in (config_overrides or {}).items():
        # Booleans render lowercase (``true``/``false``) to match the
        # engine's config grammar; everything else is stringified as-is.
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        argv.append(f"--config={key}={rendered}")
    argv.append(str(project_root))

    try:
        proc = subprocess.run(
            argv,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        partial = _to_text(exc.stdout) + _to_text(exc.stderr)
        raise TestTimeoutError(
            f"headless test run timed out after {timeout_s:.0f}s",
            partial_output=partial,
        ) from exc

    combined = proc.stdout + proc.stderr
    summary = parse_test_stream(combined)
    summary["ok"] = proc.returncode == 0 and summary["failed"] == 0
    summary["exit_code"] = proc.returncode
    return summary
