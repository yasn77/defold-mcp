#!/usr/bin/env python3
"""Harness-neutral pre-flight probe for the ``defold`` MCP server.

The probe speaks the wire protocol a harness actually uses, before any client
configuration is touched:

    initialize (protocolVersion 2025-06-18)
      -> notifications/initialized
      -> tools/list          (exactly twenty tools)
      -> tools/call defold_doctor
      -> SIGTERM             (exit within five seconds)

Two details make the result meaningful:

* **Newline-delimited JSON-RPC.** The ``mcp`` 2.x stdio transport frames one
  JSON object per line, with fd 0 and fd 1 reserved for frames; every byte of
  logging goes to stderr. Any non-JSON line on stdout is therefore a failure,
  not a detail to ignore.
* **A sanitised environment.** Only ``PATH``, ``HOME``, ``USER`` and ``TMPDIR``
  are copied from the caller's environment. Most harnesses spawn MCP servers
  with a filtered environment, so a server that only works with a full login
  shell — one that relies on ``VIRTUAL_ENV``, ``PYTHONPATH`` or an unexported
  ``DEFOLD_MCP_CACHE_DIR`` — fails here rather than silently in production.

Run it from the repository root:

    python scripts/mcp_handshake_test.py
    python scripts/mcp_handshake_test.py /path/to/defold-mcp
    mise run handshake

Exit code 0 means the handshake, the tool listing, the doctor call and the
shutdown all behaved. Any other code carries a one-line reason on stderr.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BIN = REPO_ROOT / ".venv" / "bin" / "defold-mcp"

#: The protocol version every 2025-era harness negotiates.
PROTOCOL_VERSION = "2025-06-18"

#: The complete, ordered tool set advertised by :mod:`defold_mcp.server`.
EXPECTED_TOOLS = (
    "defold_doctor",
    "defold_setup_toolchain",
    "defold_project_init",
    "defold_project_info",
    "defold_project_settings",
    "defold_sync_deps",
    "defold_list_resources",
    "defold_inspect_resource",
    "defold_mutate_resource",
    "defold_compile",
    "defold_bundle",
    "defold_preview",
    "defold_run",
    "defold_stop",
    "defold_hot_reload",
    "defold_logs",
    "defold_test_run",
    "defold_runtime_inspect",
    "defold_runtime_act",
    "defold_docs_search",
)

#: Environment variables a filtered harness environment still provides.
PASSTHROUGH_ENV = ("PATH", "HOME", "USER", "TMPDIR")

#: Seconds to wait for the initialize reply (interpreter start plus imports).
STARTUP_TIMEOUT = 60.0

#: Seconds to wait for any other reply. ``defold_doctor`` inspects the host
#: only; it never downloads, so a minute is generous without hiding a hang.
CALL_TIMEOUT = 60.0

#: Seconds the server may take to exit after SIGTERM.
SHUTDOWN_TIMEOUT = 5.0


class ProbeError(RuntimeError):
    """A failed assertion, carrying the reason to print."""


def sanitised_environment() -> dict[str, str]:
    """Build the filtered environment a harness would hand the server."""
    return {
        name: os.environ[name]
        for name in PASSTHROUGH_ENV
        if os.environ.get(name)
    }


class StdioProbe:
    """Minimal newline-delimited JSON-RPC client over a subprocess pipe."""

    def __init__(self, binary: Path) -> None:
        self.binary = binary
        self._next_id = 0
        #: Every raw stdout line, kept for the purity assertion.
        self.frames: list[str] = []
        self._stderr_tail: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self.process = subprocess.Popen(
            [str(binary), "--transport", "stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT),
            env=sanitised_environment(),
        )
        self._drain_stderr()

    # -- transport ---------------------------------------------------------

    def _drain_stderr(self) -> None:
        """Read stderr continuously so a chatty server never blocks on it."""

        def pump() -> None:
            assert self.process.stderr is not None
            for raw in self.process.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                self._stderr_tail.append(line)
                del self._stderr_tail[:-200]

        self._stderr_thread = threading.Thread(target=pump, daemon=True)
        self._stderr_thread.start()

    def stderr_excerpt(self) -> str:
        """The last few stderr lines, for a failure message."""
        return " | ".join(self._stderr_tail[-5:]) or "<no stderr output>"

    def _write(self, message: dict) -> None:
        if self.process.stdin is None:
            raise ProbeError("the server's stdin is closed")
        self.process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.process.stdin.flush()

    def _read_frame(self, deadline: float) -> dict:
        """Read one JSON-RPC frame, failing on anything that is not JSON."""
        if self.process.stdout is None:
            raise ProbeError("the server's stdout is closed")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError(
                    "timed out waiting for a server frame; stderr: "
                    f"{self.stderr_excerpt()}"
                )
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                raise ProbeError(
                    "timed out waiting for a server frame; stderr: "
                    f"{self.stderr_excerpt()}"
                )
            raw = self.process.stdout.readline()
            if not raw:
                raise ProbeError(
                    "the server closed stdout; stderr: "
                    f"{self.stderr_excerpt()}"
                )
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            self.frames.append(line)
            try:
                return json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProbeError(
                    f"stdout carried a non-JSON line ({exc}): {line[:200]!r} — "
                    "logging must go to stderr"
                ) from None

    # -- JSON-RPC ----------------------------------------------------------

    def request(
        self, method: str, params: dict | None = None, *, timeout: float
    ) -> dict:
        """Send a request and return its result, answering server pings."""
        self._next_id += 1
        request_id = self._next_id
        message: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

        deadline = time.monotonic() + timeout
        while True:
            reply = self._read_frame(deadline)
            if reply.get("id") == request_id:
                if "error" in reply:
                    raise ProbeError(
                        f"{method} returned a JSON-RPC error: {reply['error']}"
                    )
                return reply.get("result", {})
            if "method" in reply and "id" in reply:
                # A server-initiated request (e.g. ping): answer and continue.
                self._write({"jsonrpc": "2.0", "id": reply["id"], "result": {}})

    def notify(self, method: str) -> None:
        """Send a notification (no reply is expected)."""
        self._write({"jsonrpc": "2.0", "method": method})

    # -- shutdown ----------------------------------------------------------

    def terminate(self) -> float:
        """SIGTERM the server and return how long it took to exit."""
        started = time.monotonic()
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=SHUTDOWN_TIMEOUT)
            raise ProbeError(
                f"the server did not exit within {SHUTDOWN_TIMEOUT:.0f}s of SIGTERM"
            ) from None
        return time.monotonic() - started

    def close(self) -> None:
        """Release the pipes and stop the stderr pump without tracebacks."""
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=SHUTDOWN_TIMEOUT)
        # The process is dead, so the pump is at EOF: join it before the pipes
        # close, otherwise a read racing the close raises inside the thread and
        # prints a stray "Exception in thread" traceback on failure paths.
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=SHUTDOWN_TIMEOUT)
            self._stderr_thread = None
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def _check_initialize(probe: StdioProbe) -> dict:
    result = probe.request(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "defold-mcp-handshake", "version": "0.1.0"},
        },
        timeout=STARTUP_TIMEOUT,
    )
    if result.get("protocolVersion") != PROTOCOL_VERSION:
        raise ProbeError(
            "initialize negotiated protocol version "
            f"{result.get('protocolVersion')!r}, expected {PROTOCOL_VERSION!r}"
        )
    server_name = (result.get("serverInfo") or {}).get("name")
    if server_name != "defold":
        raise ProbeError(
            f"initialize reported server name {server_name!r}, expected 'defold'"
        )
    probe.notify("notifications/initialized")
    return result


def _check_tools(probe: StdioProbe) -> list[str]:
    result = probe.request("tools/list", {}, timeout=CALL_TIMEOUT)
    tools = result.get("tools") or []
    names = [tool.get("name") for tool in tools]
    if len(names) != len(EXPECTED_TOOLS):
        raise ProbeError(
            f"tools/list advertised {len(names)} tools, expected "
            f"{len(EXPECTED_TOOLS)}: {names}"
        )
    missing = [name for name in EXPECTED_TOOLS if name not in names]
    if missing:
        raise ProbeError(f"tools/list is missing {missing}")
    for tool in tools:
        if not tool.get("description"):
            raise ProbeError(f"tool {tool.get('name')!r} has no description")
        schema = tool.get("inputSchema") or {}
        if schema.get("type") != "object":
            raise ProbeError(
                f"tool {tool.get('name')!r} has a non-object input schema"
            )
    return names


def _check_doctor(probe: StdioProbe) -> str:
    result = probe.request(
        "tools/call",
        {"name": "defold_doctor", "arguments": {}},
        timeout=CALL_TIMEOUT,
    )
    if result.get("isError") is True:
        raise ProbeError(
            "defold_doctor reported an error: "
            f"{(result.get('content') or [{}])[0].get('text', '<no text>')[:300]}"
        )
    content = result.get("content") or []
    if not content:
        raise ProbeError("defold_doctor returned no content")
    text = content[0].get("text", "")
    try:
        report = json.loads(text)
    except json.JSONDecodeError:
        raise ProbeError(
            f"defold_doctor returned non-JSON content: {text[:200]!r}"
        ) from None
    if report.get("ok") is not True:
        raise ProbeError(f"defold_doctor reported ok={report.get('ok')!r}: {text[:300]}")
    if "java" not in report:
        raise ProbeError(f"defold_doctor omitted the java section: {text[:300]}")
    cache_dir = report.get("cache_dir", "?")
    return str(cache_dir)


def _check_stdout_purity(probe: StdioProbe) -> int:
    """Every frame seen must parse; drain the rest and check those too."""
    if probe.process.stdout is not None:
        for raw in probe.process.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                probe.frames.append(line)
    for frame in probe.frames:
        try:
            json.loads(frame)
        except json.JSONDecodeError as exc:
            raise ProbeError(
                f"stdout carried a non-JSON line ({exc}): {frame[:200]!r}"
            ) from None
    return len(probe.frames)


def run(binary: Path) -> int:
    """Run the whole probe and print a summary; returns the exit code."""
    if not binary.is_file():
        raise ProbeError(
            f"server binary not found at {binary}; run `mise run setup` "
            "(or pass the console script path explicitly)"
        )
    if not os.access(binary, os.X_OK):
        raise ProbeError(f"{binary} is not executable")

    environment = sanitised_environment()
    print(f"binary            {binary}")
    print(f"environment       {', '.join(sorted(environment))}")

    probe = StdioProbe(binary)
    try:
        _check_initialize(probe)
        print(f"initialize        protocolVersion {PROTOCOL_VERSION}, server 'defold'")

        names = _check_tools(probe)
        print(f"tools/list        {len(names)} tools, descriptions and schemas present")

        cache_dir = _check_doctor(probe)
        print(f"defold_doctor     ok=true, cache_dir {cache_dir}")

        elapsed = probe.terminate()
        frames = _check_stdout_purity(probe)
        print(f"SIGTERM           exited in {elapsed:.2f}s (limit {SHUTDOWN_TIMEOUT:.0f}s)")
        print(f"stdout            {frames} JSON-RPC frames, no stray text")
    finally:
        probe.close()

    print("PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point: ``main([path])``; returns the process exit code."""
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) > 1:
        print("usage: mcp_handshake_test.py [path-to-defold-mcp]", file=sys.stderr)
        return 2
    binary = Path(arguments[0]).expanduser() if arguments else DEFAULT_BIN
    try:
        return run(binary)
    except ProbeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("FAIL: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
