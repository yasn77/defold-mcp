"""Headless runner for ``bob.jar``, Defold's command-line build tool.

``bob.jar`` is invoked as ``<java> -jar <bob.jar> <commands...> <options...>``
with the PROJECT DIRECTORY as the working directory. Commands:
``resolve``, ``build``, ``bundle``, ``clean``, ``distclean``.

Diagnostic parsing
------------------
bob prints diagnostics interleaved with JVM noise on stdout. Two forms are
recognised (line-based):

* ``ERROR <resource>:<line>: <message>`` / ``WARNING <resource>:<line>: <message>``
* ``ERROR: <resource>:<line>: <message>`` / ``WARNING: <resource>:<line>: <message>``

The line number is 1-based as reported by bob; a diagnostic without a line
number (e.g. ``WARNING /main/main.collection: unable to parse``) yields
``line=None``. JDK 25 restricted-method warning blocks (the
``WARNING: A restricted method in java.lang.System has been called`` line,
its ``WARNING: Consider enabling...`` follow-up, and ``java.lang.instrument``
WARNING lines) are JVM noise and are never treated as diagnostics.

Output capture
--------------
bob prints diagnostics on stdout, but stderr is captured and concatenated
for parsing anyway: a diagnostic on either stream must not be lost, and
JVM noise is filtered by shape regardless of stream.

Timeouts
--------
``subprocess.run(..., timeout=...)`` is used. Verified against the CPython
3.14 source: on a timeout it calls ``process.kill()`` (SIGKILL on POSIX)
and ``process.wait()`` *before* re-raising ``TimeoutExpired``, so the child
is dead before :class:`BobTimeoutError` propagates. The partial output is
already populated in the exception on POSIX — but as raw **bytes** even in
text mode (the select-based ``_communicate`` joins the pipe data with
``b''.join`` in ``_check_timeout``; text decoding only happens on the
success path), so it is decoded here with UTF-8/replace.
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from defold_mcp import config, toolchain

# ``ERROR``/``WARNING`` (optional colon), a resource, an optional
# 1-based line number, then the message. The resource is matched
# non-greedily so a message containing ``: `` is preserved intact.
_DIAG_RE = re.compile(
    r"^(?P<sev>ERROR|WARNING):? (?P<resource>.+?)(?::(?P<line>\d+))?: (?P<message>.+)$"
)

# JDK 25 JVM noise that must never surface as a diagnostic.
_JVM_NOISE_RES = (
    re.compile(r"^WARNING: A restricted method in java\.lang\.System has been called"),
    re.compile(r"^WARNING: Consider enabling"),
    re.compile(r"^WARNING:.*java\.lang\.instrument"),
    re.compile(r"^WARNING: Restricted methods will be blocked"),
    re.compile(r"^WARNING: Use --enable-native-access"),
)

_JDK25_HINT = (
    "Java >= 25 is required for Defold 1.14.x but none was found; "
    "run defold_setup_toolchain(install_jdk25=True) to install a portable JDK 25, "
    "or pass java=<path> explicitly"
)


def _to_text(value: "str | bytes | None") -> str:
    """Normalise captured output to text (timeout paths yield raw bytes)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


class BobTimeoutError(RuntimeError):
    """Raised when a bob invocation exceeds its timeout.

    ``partial_output`` carries whatever the process printed before it was
    killed (the child is guaranteed dead before this is raised).
    """

    def __init__(self, message: str, *, partial_output: str = "") -> None:
        super().__init__(message)
        self.partial_output = partial_output


@dataclass
class BobResult:
    """Outcome of a bob invocation."""

    ok: bool
    exit_code: int
    diagnostics: list[dict] = field(default_factory=list)
    stdout: str = ""
    build_report: dict | None = None


def parse_bob_diagnostics(output: str) -> list[dict]:
    """Extract ``{severity, resource, line, message}`` diagnostics from bob output.

    ``severity`` is ``"error"`` or ``"warning"``; ``line`` is the 1-based
    line number reported by bob, or ``None`` when the diagnostic carries no
    line; ``message`` is the trailing text with surrounding whitespace
    stripped. JDK restricted-method warning blocks and all other
    non-diagnostic lines are ignored.
    """
    diagnostics: list[dict] = []
    for line in output.splitlines():
        if any(noise.match(line) for noise in _JVM_NOISE_RES):
            continue
        match = _DIAG_RE.match(line)
        if not match:
            continue
        diagnostics.append(
            {
                "severity": match["sev"].lower(),
                "resource": match["resource"],
                "line": int(match["line"]) if match["line"] else None,
                "message": match["message"].strip(),
            }
        )
    return diagnostics


def summarize_build_report(report: "str | bytes | Path") -> dict:
    """Summarise a ``--build-report-json`` document.

    Accepts the report as a JSON ``str``/``bytes`` or a ``Path`` to the
    report file. Shape assumptions (tolerant): the document is a JSON object
    with a ``"resources"`` list; each entry is an object with an integer
    ``"size"`` and a path-ish ``"resource"`` or ``"path"`` key. Entries
    missing those fields are skipped.

    Returns ``{"total_size": <sum over ALL resources>,
    "resources": [<top 10 by size, descending, each {path, size}>]}``.
    Raises ``ValueError`` when no ``"resources"`` list is present.
    """
    if isinstance(report, Path):
        raw = report.read_text(encoding="utf-8")
    elif isinstance(report, (bytes, bytearray)):
        raw = bytes(report).decode("utf-8", errors="replace")
    elif isinstance(report, str):
        raw = report
    else:
        raise TypeError(
            f"report must be a str, bytes, or Path, got {type(report).__name__}"
        )
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(
            "build report must be a JSON object with a 'resources' list "
            "of {resource|path, size} entries"
        )
    resources = data.get("resources")
    if not isinstance(resources, list):
        raise ValueError(
            "build report has no 'resources' list (expected a list of "
            "{resource|path, size} entries under the 'resources' key)"
        )
    entries: list[dict] = []
    for entry in resources:
        if not isinstance(entry, dict):
            continue
        size = entry.get("size")
        if not isinstance(size, int) or isinstance(size, bool):
            continue
        path = entry.get("resource", entry.get("path"))
        if path is None:
            continue
        entries.append({"path": str(path), "size": size})
    entries.sort(key=lambda item: item["size"], reverse=True)
    return {
        "total_size": sum(item["size"] for item in entries),
        "resources": entries[:10],
    }


def _resolve_java(java: "str | Path | None") -> str:
    if java is not None:
        return str(java)
    resolved = toolchain.java_executable()
    if resolved is None:
        raise RuntimeError(_JDK25_HINT)
    return str(resolved)


def _resolve_bob_jar(
    bob_jar: "str | Path | None", spec: str, client: Any
) -> str:
    if bob_jar is not None:
        return str(bob_jar)
    result = toolchain.ensure_toolchain(spec, client=client)
    if not result.get("bob_jar"):
        raise RuntimeError(
            f"toolchain for spec {spec!r} did not provide a bob.jar path"
        )
    return str(result["bob_jar"])


def _build_argv(
    java: str,
    bob_jar: str,
    commands: list[str],
    *,
    platform: "str | None",
    variant: "str | None",
    archive: bool,
    build_report_path: "str | Path | None",
    bundle_format: "str | None",
    architectures: "list[str] | str | None",
    resource_cache: bool,
) -> list[str]:
    argv = [java, "-jar", bob_jar, *commands]
    if resource_cache:
        argv += ["--resource-cache-local", str(config.get_cache_dir() / "resource-cache")]
    if platform is not None:
        argv += ["--platform", str(platform)]
    if variant is not None:
        argv += ["--variant", str(variant)]
    if archive:
        argv += ["--archive"]
    if build_report_path is not None:
        argv += ["--build-report-json", str(build_report_path)]
    if bundle_format is not None:
        argv += ["--bundle-format", str(bundle_format)]
    if architectures is not None:
        if isinstance(architectures, (list, tuple)):
            architectures = ",".join(str(a) for a in architectures)
        argv += ["--architectures", str(architectures)]
    return argv


def run_bob(
    project_root: "str | Path",
    commands: list[str],
    *,
    spec: str = "stable",
    platform: "str | None" = None,
    variant: "str | None" = None,
    archive: bool = True,
    build_report_path: "str | Path | None" = None,
    bundle_format: "str | None" = None,
    architectures: "list[str] | str | None" = None,
    resource_cache: bool = True,
    java: "str | Path | None" = None,
    bob_jar: "str | Path | None" = None,
    client: Any = None,
    timeout_s: float = 600,
) -> BobResult:
    """Run bob in ``project_root`` and return a :class:`BobResult`.

    ``java`` and ``bob_jar`` are explicit overrides; otherwise they are
    resolved via :func:`defold_mcp.toolchain.java_executable` (a clear
    ``RuntimeError`` naming the JDK 25 requirement is raised when nothing
    qualifies) and :func:`defold_mcp.toolchain.ensure_toolchain` for the
    spec's ``bob.jar``.

    ``ok`` is ``True`` when the exit code is 0 AND no ERROR diagnostics
    were parsed (warnings keep ``ok`` True). When ``build_report_path`` is
    given and the run succeeds, the report is summarised and attached; a
    missing report file yields a ``warning`` diagnostic and
    ``build_report=None`` instead of raising. A timeout raises
    :class:`BobTimeoutError` with the partial output.
    """
    argv = _build_argv(
        _resolve_java(java),
        _resolve_bob_jar(bob_jar, spec, client),
        commands,
        platform=platform,
        variant=variant,
        archive=archive,
        build_report_path=build_report_path,
        bundle_format=bundle_format,
        architectures=architectures,
        resource_cache=resource_cache,
    )
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
        raise BobTimeoutError(
            f"bob {' '.join(commands)} timed out after {timeout_s:.0f}s",
            partial_output=partial,
        ) from exc

    # bob prints diagnostics on stdout; stderr is concatenated so a
    # diagnostic on either stream is never lost (see module docstring).
    combined = proc.stdout + proc.stderr
    diagnostics = parse_bob_diagnostics(combined)

    build_report = None
    if build_report_path is not None and proc.returncode == 0:
        try:
            build_report = summarize_build_report(Path(build_report_path))
        except OSError:
            # bob exited 0 but never wrote the report (e.g. the build
            # was skipped); warn rather than crash the whole result.
            diagnostics.append(
                {
                    "severity": "warning",
                    "resource": str(build_report_path),
                    "line": None,
                    "message": "build report file not found; report not summarised",
                }
            )

    return BobResult(
        ok=proc.returncode == 0
        and not any(d["severity"] == "error" for d in diagnostics),
        exit_code=proc.returncode,
        diagnostics=diagnostics,
        stdout=combined,
        build_report=build_report,
    )
