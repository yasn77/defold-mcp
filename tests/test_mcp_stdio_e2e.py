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
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import pytest

from defold_mcp import server
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

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        request_id = self._next_id
        message: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)
        deadline = time.monotonic() + self._timeout
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

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.request(
            "tools/call", {"name": name, "arguments": arguments}
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
