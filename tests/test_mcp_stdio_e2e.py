"""Task 11 end-to-end MCP stdio protocol tests.

Part A spawns the real ``defold-mcp`` console script and speaks the SDK's
actual stdio framing (newline-delimited JSON-RPC, protocol version
``2025-06-18``) over the process pipes. The chain exercised is the Phase 6
exit criterion::

    initialize -> tools/list (exactly 20 tools) -> defold_project_init
    -> defold_project_info -> defold_mutate_resource -> defold_inspect_resource

Part B drives :mod:`defold_mcp.server` in-process for behaviour the stdio
chain cannot reach: resource-path containment, the 25,000-character
response cap, editor-vs-AST mutation routing, engine lifecycle on
repeated ``defold_run`` calls, and the spawned-process log offsets.

Every test is hermetic: no network, no Editor, no engine and no toolchain
download.

The installed SDK is ``mcp`` 2.x, where ``FastMCP`` was renamed to
``MCPServer``; the stdio transport frames messages as one JSON object per
line (see ``mcp/server/stdio.py``).
"""

import json
import os
import select
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import pytest

from defold_mcp import bob_runner, docs_index, server
from defold_mcp.textproto import (
    decode_embedded_data,
    parse_textproto,
    serialize_textproto,
)
from defold_mcp.textproto.schemas import ALWAYS_LIST_FIELDS

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN = REPO_ROOT / ".venv" / "bin" / "defold-mcp"

SERVER_NAME = "defold"

# Exact tool names in plan section 2.3 order.
EXPECTED_TOOLS = [
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
]

INITIALIZE_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "defold-mcp-e2e", "version": "0.0.0"},
}


class StdioClient:
    """Minimal newline-delimited JSON-RPC client over a subprocess pipe."""

    def __init__(self, proc: subprocess.Popen, timeout: float = 30.0):
        self._proc = proc
        self._timeout = timeout
        self._next_id = 0
        #: Every raw stdout line seen, for the stdout-purity assertion.
        self.frames: list[str] = []

    # -- transport ---------------------------------------------------------

    def _write(self, message: dict) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self._proc.stdin.flush()

    def _read_message(self, deadline: float) -> dict:
        assert self._proc.stdout is not None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "timed out waiting for a server frame; stderr: "
                    f"{self._stderr_tail()!r}"
                )
            ready, _, _ = select.select([self._proc.stdout], [], [], remaining)
            if not ready:
                raise TimeoutError(
                    "timed out waiting for a server frame; stderr: "
                    f"{self._stderr_tail()!r}"
                )
            line = self._proc.stdout.readline()
            if not line:
                raise ConnectionError(
                    f"server closed stdout; stderr: {self._stderr_tail()!r}"
                )
            raw = line.decode("utf-8").strip()
            if raw:
                self.frames.append(raw)
                return json.loads(raw)

    def _stderr_tail(self) -> str:
        assert self._proc.stderr is not None
        try:
            return self._proc.stderr.read()[-2000:].decode("utf-8", "replace")
        except Exception:
            return "<unreadable>"

    # -- JSON-RPC ----------------------------------------------------------

    def request(
        self, method: str, params: dict | None = None, *, timeout: float | None = None
    ) -> dict:
        self._next_id += 1
        request_id = self._next_id
        message: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)
        deadline = time.monotonic() + (
            self._timeout if timeout is None else timeout
        )
        while True:
            reply = self._read_message(deadline)
            if reply.get("id") == request_id:
                if "error" in reply:
                    raise AssertionError(
                        f"{method} returned a JSON-RPC error: {reply['error']}"
                    )
                return reply["result"]
            if "method" in reply and "id" in reply:
                # Server-to-client request (e.g. ping): answer and continue.
                self._write({"jsonrpc": "2.0", "id": reply["id"], "result": {}})

    def notify(self, method: str, params: dict | None = None) -> None:
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def call_tool(
        self, name: str, arguments: dict, *, timeout: float | None = None
    ) -> dict:
        return self.request(
            "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
        )

    # -- shutdown ----------------------------------------------------------

    def close(self) -> None:
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        # Release the read pipes; leaving them to the garbage collector
        # trips a ResourceWarning under strict warning filters.
        for stream in (self._proc.stdout, self._proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def _spawn() -> subprocess.Popen:
    assert BIN.is_file(), f"defold-mcp console script missing at {BIN}"
    return subprocess.Popen(
        [str(BIN), "--transport", "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
    )


@pytest.fixture(scope="module")
def client():
    proc = _spawn()
    stdio = StdioClient(proc)
    try:
        yield stdio
    finally:
        stdio.close()


@pytest.fixture(scope="module")
def project_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("defold-project")


def _tool_json(result: dict) -> dict:
    """Parse the JSON text payload of a successful ``tools/call`` result."""
    assert result.get("isError") in (None, False), result
    return json.loads(result["content"][0]["text"])


def _assert_round_trips(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    tree = parse_textproto(text, schema=ALWAYS_LIST_FIELDS)
    assert serialize_textproto(decode_embedded_data(tree)) == text, (
        f"{path} does not round-trip byte-exactly"
    )


def _init_project(client: StdioClient, target: Path, title: str) -> dict:
    result = client.call_tool(
        "defold_project_init",
        {"project_root": str(target), "title": title, "profile": "2d_pixel"},
    )
    return _tool_json(result)


# ---------------------------------------------------------------------------
# Part A: end-to-end tests over stdio
# ---------------------------------------------------------------------------


def test_initialize_and_tools_list(client: StdioClient) -> None:
    result = client.request("initialize", INITIALIZE_PARAMS)
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == SERVER_NAME
    client.notify("notifications/initialized")

    listing = client.request("tools/list", {})
    names = [tool["name"] for tool in listing["tools"]]
    assert names == EXPECTED_TOOLS
    for tool in listing["tools"]:
        assert tool["description"], f"{tool['name']} has no description"
        assert isinstance(tool["inputSchema"], dict)
        assert tool["inputSchema"].get("type") == "object"


def test_project_init(client: StdioClient, project_dir: Path) -> None:
    target = project_dir / "quest"
    data = _init_project(client, target, "TestQuest")
    assert data["ok"] is True
    assert data["project_root"] == str(target)
    assert data["profile"] == "2d_pixel"

    game_project = (target / "game.project").read_text(encoding="utf-8")
    assert 'title = "TestQuest"' in game_project

    _assert_round_trips(target / "main" / "main.collection")
    assert (target / ".gitignore").is_file()
    assert (target / ".defignore").is_file()
    assert (target / "license" / "defold_notice.lua").is_file()


def test_project_init_refuses_a_second_run(
    client: StdioClient, project_dir: Path
) -> None:
    target = project_dir / "quest"
    again = client.call_tool(
        "defold_project_init",
        {"project_root": str(target), "title": "Again", "profile": "2d_pixel"},
    )
    assert again.get("isError") is True


def test_project_info(client: StdioClient, project_dir: Path) -> None:
    target = project_dir / "info-proj"
    _init_project(client, target, "Info Game")
    data = _tool_json(
        client.call_tool(
            "defold_project_info", {"project_root": str(target)}
        )
    )
    assert data["title"] == "Info Game"
    assert data["backend"] == "headless"
    assert data["project_root"] == str(target)
    assert isinstance(data["resources"], list)
    assert data["resources"], "expected at least one resource"
    paths = {entry["path"] for entry in data["resources"]}
    assert "main/main.collection" in paths


def test_mutate_resource(client: StdioClient, project_dir: Path) -> None:
    target = project_dir / "mutate-proj"
    _init_project(client, target, "Mutate Game")

    # (a) create a .go with an embedded sprite and a referenced script.
    created = _tool_json(
        client.call_tool(
            "defold_mutate_resource",
            {
                "project_root": str(target),
                "resource_path": "/main/hero.go",
                "operations": [
                    {"op": "create", "resource_type": "go"},
                    {
                        "op": "add_component",
                        "id": "sprite",
                        "type": "sprite",
                        "embedded": True,
                        "data": (
                            'default_animation: "idle"\n'
                            'material: "/builtins/materials/sprite.material"\n'
                        ),
                    },
                    {
                        "op": "add_component",
                        "id": "script",
                        "component": "/main/main.script",
                    },
                ],
            },
        )
    )
    assert created["ok"] is True
    assert created["backend"] == "headless"

    hero_path = target / "main" / "hero.go"
    _assert_round_trips(hero_path)
    hero = parse_textproto(
        hero_path.read_text(encoding="utf-8"), schema=ALWAYS_LIST_FIELDS
    )
    assert hero["embedded_components"][0]["type"] == "sprite"
    assert hero["components"][0]["component"] == "/main/main.script"

    # (b) instance the new .go into the project's main collection.
    instanced = _tool_json(
        client.call_tool(
            "defold_mutate_resource",
            {
                "project_root": str(target),
                "resource_path": "/main/main.collection",
                "operations": [
                    {
                        "op": "add_instance",
                        "id": "hero",
                        "prototype": "/main/hero.go",
                    }
                ],
            },
        )
    )
    assert instanced["ok"] is True

    collection_path = target / "main" / "main.collection"
    _assert_round_trips(collection_path)
    collection = parse_textproto(
        collection_path.read_text(encoding="utf-8"), schema=ALWAYS_LIST_FIELDS
    )
    assert any(entry["id"] == "hero" for entry in collection["instances"])


def test_inspect_resource(client: StdioClient, project_dir: Path) -> None:
    target = project_dir / "mutate-proj"
    data = _tool_json(
        client.call_tool(
            "defold_inspect_resource",
            {
                "project_root": str(target),
                "resource_path": "/main/main.collection",
            },
        )
    )
    assert data["path"] == "/main/main.collection"
    assert isinstance(data["tree"], dict)
    assert any(entry["id"] == "hero" for entry in data["tree"]["instances"])


def test_inspect_resource_lua_outline(
    client: StdioClient, project_dir: Path
) -> None:
    target = project_dir / "info-proj"
    data = _tool_json(
        client.call_tool(
            "defold_inspect_resource",
            {
                "project_root": str(target),
                "resource_path": "/main/main.script",
            },
        )
    )
    assert data["path"] == "/main/main.script"
    assert data["outline"]["functions"] == ["init", "update"]


def test_stdout_is_json_only_and_sigterm_exits() -> None:
    proc = _spawn()
    stdio = StdioClient(proc)
    try:
        stdio.request("initialize", INITIALIZE_PARAMS)
        stdio.notify("notifications/initialized")
        stdio.request("tools/list", {})
        stdio.call_tool("defold_doctor", {})
        proc.terminate()
        assert proc.wait(timeout=5) is not None
        assert proc.stdout is not None
        trailing = proc.stdout.read().decode("utf-8")
    finally:
        stdio.close()

    frames = list(stdio.frames)
    frames += [line for line in trailing.splitlines() if line.strip()]
    assert frames, "expected at least one JSON-RPC frame on stdout"
    for frame in frames:
        json.loads(frame)


# ---------------------------------------------------------------------------
# Part B: in-process server unit tests
# ---------------------------------------------------------------------------


class _RecordingEditor:
    """Stand-in for a live Editor that records bridge traffic."""

    def __init__(self):
        self.transact_calls = 0

    def transact_mutations(self, operations, resource_path):
        self.transact_calls += 1
        return {"ok": True}


def _scaffold(tmp_path: Path, name: str = "unit-proj") -> Path:
    """Create a real (hermetic) project on disk and return its root."""
    root = tmp_path / name
    server.defold_project_init(
        str(root), title="Unit", profile="minimal"
    )
    return root


def _no_editor(monkeypatch) -> None:
    monkeypatch.setattr(
        server.editor_client, "probe_editor", lambda *a, **k: None
    )


# -- resource-path containment (CRITICAL) -----------------------------------


@pytest.mark.parametrize(
    "resource_path",
    ["/../outside.go", "/main/../../outside.go", "../../outside.go"],
)
def test_inspect_resource_rejects_parent_traversal(tmp_path, resource_path):
    root = _scaffold(tmp_path, "containment")
    outside = tmp_path / "outside.go"
    outside.write_text('name: "secret"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="escapes the project root"):
        server.defold_inspect_resource(
            resource_path, project_root=str(root)
        )


def test_mutate_resource_rejects_parent_traversal(tmp_path):
    root = _scaffold(tmp_path, "containment-mutate")
    outside = tmp_path / "outside.collection"
    outside.write_text('name: "secret"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="escapes the project root"):
        server.defold_mutate_resource(
            "/../outside.collection",
            [{"op": "set_property", "parent": "", "key": "name", "value": "x"}],
            project_root=str(root),
        )
    # The file outside the root must be untouched.
    assert outside.read_text(encoding="utf-8") == 'name: "secret"\n'


def test_resource_path_accepts_a_nested_subpath(tmp_path):
    root = _scaffold(tmp_path, "containment-ok")
    nested = root / "main" / "nested"
    nested.mkdir()
    (nested / "hero.go").write_text(
        'embedded_components {\n  id: "sprite"\n  type: "sprite"\n'
        '  data: "x: 1\\n"\n}\n',
        encoding="utf-8",
    )

    data = json.loads(
        server.defold_inspect_resource(
            "/main/nested/hero.go", project_root=str(root)
        )
    )
    assert data["ok"] is True
    assert data["tree"]["embedded_components"][0]["id"] == "sprite"


# -- response cap (MAJOR) ---------------------------------------------------


def test_truncate_caps_an_escaping_heavy_payload():
    payload = {"ok": True, "content": '\\"' * 20_000}
    text = server._truncate(payload)
    assert len(text) <= server.MAX_RESPONSE_CHARS
    assert json.loads(text)["truncated"] is True


def test_truncate_caps_a_multiline_payload():
    payload = {"ok": True, "content": "\n".join("x" * 80 for _ in range(800))}
    text = server._truncate(payload)
    assert len(text) <= server.MAX_RESPONSE_CHARS
    assert json.loads(text)["truncated"] is True


def test_truncate_leaves_a_small_payload_intact():
    payload = {"ok": True, "content": "small"}
    text = server._truncate(payload)
    assert json.loads(text) == payload


def test_inspect_resource_stays_within_the_response_cap(tmp_path):
    root = _scaffold(tmp_path, "big-proj")
    instances = "".join(
        f'instances {{\n  id: "i{i}"\n  prototype: "/main/hero.go"\n}}\n'
        for i in range(1200)
    )
    (root / "main" / "big.collection").write_text(
        'name: "big"\n' + instances, encoding="utf-8"
    )

    text = server.defold_inspect_resource(
        "/main/big.collection", project_root=str(root)
    )
    assert len(text) <= server.MAX_RESPONSE_CHARS
    assert json.loads(text)["truncated"] is True


# -- editor-vs-AST mutation routing (MAJOR) ---------------------------------


def test_mutate_routes_registry_set_property_to_the_ast_path(
    tmp_path, monkeypatch
):
    """A registry-dialect ``set_property`` must never reach the Editor bridge.

    The Editor dialect spells ``set_property`` as ``node``/``key``/``value``;
    the registry spells it ``parent``/``key``/``value``. Routing on the op
    name alone would raise inside ``transact_mutations`` where the AST path
    succeeds.
    """
    root = _scaffold(tmp_path, "routing")
    editor = _RecordingEditor()
    monkeypatch.setattr(
        server.editor_client, "probe_editor", lambda *a, **k: editor
    )

    data = json.loads(
        server.defold_mutate_resource(
            "/main/main.collection",
            [
                {
                    "op": "set_property",
                    "parent": "",
                    "key": "name",
                    "value": "renamed",
                }
            ],
            project_root=str(root),
        )
    )
    assert data["ok"] is True
    assert data["backend"] == "headless"
    assert editor.transact_calls == 0

    tree = parse_textproto(
        (root / "main" / "main.collection").read_text(encoding="utf-8"),
        schema=ALWAYS_LIST_FIELDS,
    )
    assert tree["name"] == "renamed"


# -- engine lifecycle (MAJOR) ----------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - pid reuse by another user
        return True
    return True


def _fake_engine(tmp_path: Path, port: int = 8123) -> Path:
    """A stand-in ``dmengine`` that announces a port and then idles."""
    script = tmp_path / "fake_engine.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"echo 'INFO:ENGINE: Engine service started on port {port}'\n"
        "sleep 30\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_defold_run_replaces_a_live_engine(tmp_path, monkeypatch):
    root = _scaffold(tmp_path, "run-proj")
    engine = _fake_engine(tmp_path)
    _no_editor(monkeypatch)
    monkeypatch.setattr(
        server.toolchain,
        "ensure_toolchain",
        lambda *a, **k: {"dmengine": str(engine)},
    )

    try:
        first = json.loads(
            server.defold_run(project_root=str(root), compile_first=False)
        )
        second = json.loads(
            server.defold_run(project_root=str(root), compile_first=False)
        )
        assert first["pid"] != second["pid"]
        assert _pid_alive(second["pid"])
        assert not _pid_alive(first["pid"]), "the first engine was orphaned"
    finally:
        server.defold_stop(project_root=str(root))


# -- spawned-process log offsets (MAJOR) ------------------------------------


def _fake_run_entry(lines: list[str]) -> dict:
    return {
        "ring": deque(lines, maxlen=server._RING_CAPACITY),
        "total": len(lines),
        "lock": threading.Lock(),
    }


def test_process_log_slice_is_monotone_under_a_filter():
    entry = _fake_run_entry(["keep 1", "drop", "keep 2", "drop", "keep 3"])

    first = server._slice_ring(entry, 0, 2, "keep")
    assert first["lines"] == ["keep 1"]
    assert first["next_offset"] == 2

    second = server._slice_ring(entry, first["next_offset"], 2, "keep")
    assert second["lines"] == ["keep 2"]
    assert second["next_offset"] == 4

    third = server._slice_ring(entry, second["next_offset"], 2, "keep")
    assert third["lines"] == ["keep 3"]
    # The window is [4, 6) clipped to the 5 retained lines.
    assert third["next_offset"] == 5


def test_process_log_slice_survives_ring_eviction():
    lines = [f"line {index}" for index in range(5_300)]
    entry = _fake_run_entry(lines)

    result = server._slice_ring(entry, 0, 100, None)
    # 300 lines were evicted, so the read starts at absolute offset 300 and
    # next_offset is 400 — not 100.
    assert result["lines"][0] == "line 300"
    assert len(result["lines"]) == 100
    assert result["next_offset"] == 400

    tail = server._slice_ring(entry, result["next_offset"], 100, None)
    assert tail["lines"][0] == "line 400"
    assert tail["next_offset"] == 500


def test_defold_stop_without_a_run_omits_backend(tmp_path):
    root = _scaffold(tmp_path, "stop-proj")
    data = json.loads(server.defold_stop(project_root=str(root)))
    assert data["ok"] is False
    assert "backend" not in data


# -- documentation index self-healing (live-phase finding) ------------------


def test_docs_db_needs_build_when_missing(tmp_path):
    assert server._docs_db_needs_build(tmp_path / "docs.db") is True


def test_docs_db_needs_build_when_populated(tmp_path):
    db_path = tmp_path / "docs.db"
    index = docs_index.DocsIndex(str(db_path))
    try:
        index.ingest_manual("intro", "Intro", "body text")
    finally:
        index.close()
    assert server._docs_db_needs_build(db_path) is False


def test_docs_db_needs_build_when_empty(tmp_path):
    # An interrupted ingest leaves a valid-but-empty FTS table; treating it
    # as "already built" would make every later search return nothing.
    db_path = tmp_path / "docs.db"
    docs_index.DocsIndex(str(db_path)).close()
    assert server._docs_db_needs_build(db_path) is True


# -- test-run summary passthrough (live-phase finding) ---------------------


def test_test_run_surfaces_run_id_and_exit_code(tmp_path, monkeypatch):
    root = _scaffold(tmp_path, "test-run-proj")
    monkeypatch.setattr(
        server.test_runner,
        "build_test_collection",
        lambda *a, **k: bob_runner.BobResult(ok=True, exit_code=0),
    )
    monkeypatch.setattr(
        server.test_runner,
        "run_headless_tests",
        lambda *a, **k: {
            "ok": True,
            "run": "smoke1",
            "status": "pass",
            "passed": 2,
            "failed": 0,
            "tests": [],
            "rejected_events": 0,
            "stdout_tail": "TEST {...}",
            "exit_code": 0,
        },
    )

    data = json.loads(
        server.defold_test_run(
            "/test/math_test.collection", project_root=str(root)
        )
    )
    assert data["ok"] is True
    assert data["run"] == "smoke1"
    assert data["status"] == "pass"
    assert data["passed"] == 2
    assert data["failed"] == 0
    assert data["exit_code"] == 0


def test_test_run_builds_the_collection_before_running_it(tmp_path, monkeypatch):
    root = _scaffold(tmp_path, "test-build-proj")
    order: list[str] = []

    def fake_build(*args, **kwargs):
        order.append("build")
        return bob_runner.BobResult(ok=True, exit_code=0)

    def fake_run(*args, **kwargs):
        order.append("run")
        return {
            "ok": True,
            "run": "r1",
            "status": "pass",
            "passed": 1,
            "failed": 0,
            "tests": [],
            "rejected_events": 0,
            "stdout_tail": "",
            "exit_code": 0,
        }

    monkeypatch.setattr(server.test_runner, "build_test_collection", fake_build)
    monkeypatch.setattr(server.test_runner, "run_headless_tests", fake_run)

    json.loads(
        server.defold_test_run("/test/t.collection", project_root=str(root))
    )
    assert order == ["build", "run"]


def test_test_run_reports_a_failed_build_without_running_the_engine(
    tmp_path, monkeypatch
):
    root = _scaffold(tmp_path, "test-build-fail")
    monkeypatch.setattr(
        server.test_runner,
        "build_test_collection",
        lambda *a, **k: bob_runner.BobResult(
            ok=False,
            exit_code=1,
            diagnostics=[
                {
                    "severity": "error",
                    "resource": "/test/t.collection",
                    "line": None,
                    "message": "the collection can't be found",
                }
            ],
            stdout="SEVERE  the collection can't be found\n",
        ),
    )
    ran: list[int] = []
    monkeypatch.setattr(
        server.test_runner, "run_headless_tests", lambda *a, **k: ran.append(1)
    )

    data = json.loads(
        server.defold_test_run("/test/t.collection", project_root=str(root))
    )
    assert data["ok"] is False
    assert data["diagnostics"], data
    assert ran == [], "the engine must not run when the build failed"


# ---------------------------------------------------------------------------
# Part C: live smoke test against the real Defold 1.14.1 toolchain
# ---------------------------------------------------------------------------

#: Scratch project for the live run (allowed temp location, wiped per run).
LIVE_SCRATCH = Path("/tmp/opencode/defold-mcp-live-smoke")

#: Per-call ceiling for the live run: the toolchain download is ~500 MB and
#: the two real builds take minutes on a cold cache.
LIVE_TIMEOUT = 1_800.0

#: The script the engine runs: emits one TEST ``suite_end`` event and asks
#: the engine to exit cleanly.
LIVE_RUNNER_SCRIPT = (
    "function init(self)\n"
    '    print(\'TEST {"run": "smoke1", "event": "suite_end", '
    '"status": "pass", "passed": 2, "failed": 0}\')\n'
    '    msg.post("@system:", "exit", { code = 0 })\n'
    "end\n"
)


def _live_call(
    client: StdioClient,
    name: str,
    arguments: dict,
    *,
    timeout: float = LIVE_TIMEOUT,
) -> dict:
    """Call a live tool with a generous per-call timeout."""
    return _tool_json(client.call_tool(name, arguments, timeout=timeout))


@pytest.mark.live
def test_live_defold_1_14_1_build_and_headless_test(client: StdioClient) -> None:
    """Drive the real server against the real Defold 1.14.1 toolchain.

    Nothing is mocked: the toolchain (Temurin JDK 25, ``bob.jar``, the
    headless engine, builtins and the pinned ref-doc) is downloaded, the
    FTS documentation index is ingested from the network, a project is
    scaffolded, a reachable test collection is compiled by ``bob``, run in
    ``dmengine_headless`` and finally bundled for ``wasm-web``.
    """
    client.request("initialize", INITIALIZE_PARAMS)
    client.notify("notifications/initialized")

    # 1. Toolchain: JDK 25, bob.jar, headless engine, builtins, ref-doc and
    #    the documentation index.
    setup = _live_call(
        client,
        "defold_setup_toolchain",
        {"spec": "1.14.1", "install_jdk25": True},
    )
    assert setup["ok"] is True
    assert setup["version"] == config.DEFAULT_DEFOLD_VERSION
    assert setup["sha1"], "the pinned spec must resolve to a build sha1"

    bob_jar = Path(setup["bob_jar"])
    assert bob_jar.is_file()
    assert bob_jar.read_bytes()[:2] == b"PK", "bob.jar is not a zip"
    headless = Path(setup["dmengine_headless"])
    assert headless.is_file()
    assert os.access(headless, os.X_OK), "the headless engine is not executable"
    assert Path(setup["builtins_zip"]).is_file()
    assert Path(setup["ref_doc_zip"]).is_file()
    assert Path(setup["engine_dir"]).is_dir()

    doctor = _live_call(client, "defold_doctor", {})
    assert doctor["java"]["ok"] is True, doctor["java"]
    assert doctor["java"]["major"] == 25, doctor["java"]
    assert doctor["docs"], "the documentation index is empty after setup"
    assert "lua_api" in doctor["docs"], doctor["docs"]

    # 2. The real ref-doc reached the FTS index.
    search = _live_call(
        client,
        "defold_docs_search",
        {"action": "search", "query": "go.animate", "scope": "lua_api"},
    )
    assert any(
        hit["doc_id"] == "lua_api:go.animate" for hit in search["results"]
    ), search["results"][:5]

    doc = _live_call(
        client,
        "defold_docs_search",
        {"action": "get", "doc_id": "lua_api:go.animate"},
    )
    assert "go.animate(" in doc["content"], doc["content"][:400]

    # 3. Scaffold the project and add a test collection bob can reach.
    if LIVE_SCRATCH.exists():
        shutil.rmtree(LIVE_SCRATCH)
    LIVE_SCRATCH.parent.mkdir(parents=True, exist_ok=True)

    init = _live_call(
        client,
        "defold_project_init",
        {
            "project_root": str(LIVE_SCRATCH),
            "profile": "2d_pixel",
            "title": "SmokeQuest",
        },
    )
    assert init["ok"] is True
    assert (LIVE_SCRATCH / "game.project").is_file()

    runner = LIVE_SCRATCH / "test" / "runner.script"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text(LIVE_RUNNER_SCRIPT, encoding="utf-8")

    # The script component is REFERENCED (scripts cannot be embedded), so
    # the collection carries ``component: "/test/runner.script"``.
    collection = _live_call(
        client,
        "defold_mutate_resource",
        {
            "project_root": str(LIVE_SCRATCH),
            "resource_path": "/test/math_test.collection",
            "operations": [
                {
                    "op": "set_property",
                    "parent": "",
                    "key": "name",
                    "value": "math_test",
                },
                {
                    "op": "add_embedded_instance",
                    "id": "test_runner",
                    "components": [
                        {"id": "script", "component": "/test/runner.script"}
                    ],
                },
            ],
        },
    )
    assert collection["ok"] is True
    _assert_round_trips(LIVE_SCRATCH / "test" / "math_test.collection")

    # bob compiles only the graph reachable from the bootstrap collection,
    # so the test collection must be instanced into ``main.collection`` as
    # a subcollection or it is never compiled.
    subcollection = _live_call(
        client,
        "defold_mutate_resource",
        {
            "project_root": str(LIVE_SCRATCH),
            "resource_path": "/main/main.collection",
            "operations": [
                {
                    "op": "add_subcollection",
                    "id": "test",
                    "collection": "/test/math_test.collection",
                    "position": {"x": 0.0, "y": 0.0},
                }
            ],
        },
    )
    assert subcollection["ok"] is True
    main_text = (LIVE_SCRATCH / "main" / "main.collection").read_text(
        encoding="utf-8"
    )
    # ``collection_instances.collection`` carries the SOURCE path; a
    # ``.collectionc`` path fails bob with "the collection ... can't be
    # found" (verified against Defold 1.14.1).
    assert "/test/math_test.collection" in main_text, main_text
    assert "/test/math_test.collectionc" not in main_text, main_text
    _assert_round_trips(LIVE_SCRATCH / "main" / "main.collection")

    # 4. A real ``bob build`` under JDK 25.
    compile_result = _live_call(
        client,
        "defold_compile",
        {"project_root": str(LIVE_SCRATCH), "variant": "debug"},
    )
    assert compile_result["ok"] is True, compile_result["diagnostics"]
    assert compile_result["diagnostics"] == [], compile_result["diagnostics"]

    compiled = list((LIVE_SCRATCH / "build").rglob("math_test.collectionc"))
    assert compiled, "the test collection was never compiled"

    # 5. A real headless test run.
    test_result = _live_call(
        client,
        "defold_test_run",
        {
            "test_collection": "/test/math_test.collection",
            "project_root": str(LIVE_SCRATCH),
            "timeout_s": 60,
        },
        timeout=300.0,
    )
    assert test_result["ok"] is True, test_result
    assert test_result["run"] == "smoke1", test_result
    assert test_result["passed"] == 2, test_result
    assert test_result["failed"] == 0, test_result
    assert test_result["exit_code"] == 0, test_result

    # 6. A real wasm-web bundle.
    bundle = _live_call(
        client,
        "defold_bundle",
        {
            "platform": "wasm-web",
            "variant": "release",
            "project_root": str(LIVE_SCRATCH),
        },
    )
    assert bundle["ok"] is True, bundle["diagnostics"]

    build_dir = LIVE_SCRATCH / "build"
    assert list(build_dir.rglob("index.html")), "no index.html in the bundle"
    wasm = [p for p in build_dir.rglob("*.wasm") if p.stat().st_size > 0]
    assert wasm, "no non-empty .wasm in the bundle"
    arcd = [p for p in build_dir.rglob("game.arcd") if p.stat().st_size > 0]
    assert arcd, "no non-empty game.arcd in the bundle"
