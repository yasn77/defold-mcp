"""Hermetic tests for the Defold Editor HTTP client.

All HTTP goes through ``httpx.MockTransport``; no test touches a real
Editor, the network, or a JVM.
"""

import json
import time
from pathlib import Path

import httpx
import pytest

from defold_mcp import editor_client
from defold_mcp.editor_client import (
    EditorClient,
    EditorClientError,
    LuaScriptError,
    lua_quote,
    probe_editor,
)

FIXTURES = Path(__file__).parent / "fixtures"
OPENAPI = json.loads((FIXTURES / "editor_openapi.json").read_text(encoding="utf-8"))
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"pretend-png-payload"
TOKEN = "TEST_TOKEN_abc123"
PORT = 8001
BASE = f"http://127.0.0.1:{PORT}"


# -- helpers -----------------------------------------------------------------


class MockEditor:
    """Scriptable MockTransport handler with request capture.

    ``set(method, path, response)`` registers a fixed response; pass a list
    of responses to script a sequence (popped in order).
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], object] = {}
        self.requests: list[httpx.Request] = []

    def set(self, method: str, path: str, response: object) -> None:
        self.routes[(method.upper(), path)] = response

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        value = self.routes.get(key)
        if value is None:
            raise AssertionError(f"unexpected request {request.method} {request.url.path}")
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"exhausted scripted responses for {key}")
            return value.pop(0)
        return value

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]


def make_project(
    tmp_path: Path,
    *,
    port: int = PORT,
    token: str = TOKEN,
    port_file: bool = True,
    token_file: bool = True,
) -> Path:
    """Write ``.internal/editor.port`` and ``.internal/editor.token``."""
    internal = tmp_path / ".internal"
    internal.mkdir(parents=True, exist_ok=True)
    if port_file:
        (internal / "editor.port").write_text(str(port), encoding="utf-8")
    if token_file:
        (internal / "editor.token").write_text(token, encoding="utf-8")
    return tmp_path


@pytest.fixture
def mock_editor() -> MockEditor:
    ed = MockEditor()
    ed.set("GET", "/openapi.json", httpx.Response(200, json=OPENAPI))
    return ed


@pytest.fixture
def make_client(mock_editor, tmp_path):
    def _make(**kwargs):
        project = make_project(tmp_path, **kwargs)
        transport = httpx.MockTransport(mock_editor.handler)
        return probe_editor(project, client=httpx.Client(transport=transport))

    return _make


# -- probe -------------------------------------------------------------------


def test_probe_returns_none_when_port_file_missing(tmp_path, mock_editor):
    client = httpx.Client(transport=httpx.MockTransport(mock_editor.handler))
    assert probe_editor(tmp_path, client=client) is None


def test_probe_returns_none_on_connection_refused(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    project = make_project(tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert probe_editor(project, client=client) is None


def test_probe_returns_client_on_openapi_ok(make_client):
    client = make_client()
    assert isinstance(client, EditorClient)
    assert client.base_url == BASE
    assert client.spec_version == "1.14.1"
    assert client._token == TOKEN


# -- compile -----------------------------------------------------------------


def test_compile_ok_and_422_issues(make_client, mock_editor):
    mock_editor.set(
        "POST",
        "/command/compile",
        [
            httpx.Response(200, json={"success": True, "issues": []}),
            httpx.Response(
                422,
                json={
                    "success": False,
                    "issues": [
                        {
                            "message": "Unknown property 'bogus'",
                            "severity": "error",
                            "resource": "/main/main.collection",
                            "range": {
                                "start": {"line": 3, "character": 5},
                                "end": {"line": 3, "character": 12},
                            },
                        }
                    ],
                },
            ),
        ],
    )
    client = make_client()

    ok = client.compile()
    assert ok == {"ok": True, "issues": []}

    failed = client.compile()
    assert failed["ok"] is False
    issue = failed["issues"][0]
    # 0-based range values must be converted to 1-based line/col.
    assert issue["line"] == 4
    assert issue["col"] == 6
    assert issue["end_line"] == 4
    assert issue["end_col"] == 13
    assert issue["message"] == "Unknown property 'bogus'"
    assert issue["severity"] == "error"
    assert issue["resource"] == "/main/main.collection"


def test_compile_422_non_dict_body_raises_editor_error(make_client, mock_editor):
    mock_editor.set("POST", "/command/compile", httpx.Response(422, json=["not", "a", "dict"]))
    client = make_client()
    with pytest.raises(EditorClientError) as excinfo:
        client.compile()
    assert excinfo.value.status == 422


def test_compile_422_malformed_range_fields_coerced_to_none(make_client, mock_editor):
    mock_editor.set(
        "POST",
        "/command/compile",
        httpx.Response(
            422,
            json={
                "issues": [
                    {
                        "message": "bad range",
                        "severity": "error",
                        "resource": "/main/main.collection",
                        "range": {
                            "start": {"line": "three", "character": None},
                            "end": {"line": True, "character": 5.5},
                        },
                    }
                ]
            },
        ),
    )
    client = make_client()
    failed = client.compile()
    issue = failed["issues"][0]
    assert issue["line"] is None
    assert issue["col"] is None
    assert issue["end_line"] is None
    assert issue["end_col"] is None


# -- command POSTs -----------------------------------------------------------


def test_commands_post(make_client, mock_editor):
    mock_editor.set("POST", "/command/run", httpx.Response(200, json={"launched": True}))
    mock_editor.set("POST", "/command/hot-reload", httpx.Response(200))
    mock_editor.set("POST", "/command/fetch-libraries", httpx.Response(200))
    mock_editor.set("POST", "/command/clean-build", httpx.Response(200))
    mock_editor.set(
        "POST",
        "/command/build-html5",
        httpx.Response(200, json={"url": f"{BASE}/html5/"}),
    )
    mock_editor.set("POST", "/command/reload-extensions", httpx.Response(200))
    client = make_client()

    assert client.run() == {"ok": True, "launched": True}
    run_req = mock_editor.sent("POST", "/command/run")[0]
    assert dict(run_req.url.params) == {"focus": "false"}

    assert client.run(focus=True) == {"ok": True, "launched": True}
    assert dict(mock_editor.sent("POST", "/command/run")[1].url.params) == {"focus": "true"}

    assert client.hot_reload() == {"ok": True}
    assert client.fetch_libraries() == {"ok": True}
    assert client.clean_build() == {"ok": True}
    assert client.reload_extensions() == {"ok": True}

    html5 = client.build_html5()
    assert html5["ok"] is True
    assert html5["url"] == f"{BASE}/html5/"


# -- preview -----------------------------------------------------------------


def test_render_preview_strips_slash_and_writes_png(make_client, mock_editor, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setenv("DEFOLD_MCP_CACHE_DIR", str(cache))
    mock_editor.set(
        "GET", "/preview/main/main.collection", httpx.Response(200, content=PNG_BYTES)
    )
    client = make_client()

    out = client.render_preview("/main/main.collection")
    req = mock_editor.sent("GET", "/preview/main/main.collection")[0]
    assert dict(req.url.params) == {"width": "1280", "height": "720"}
    assert out == cache / "previews" / "main.collection-1280x720.png"
    assert out.read_bytes()[:4] == b"\x89PNG"

    custom = tmp_path / "custom.png"
    out2 = client.render_preview("main/main.collection", out_path=custom)
    assert out2 == custom
    assert custom.read_bytes() == PNG_BYTES


def test_render_preview_404_and_422(make_client, mock_editor):
    mock_editor.set(
        "GET", "/preview/main/missing.go", httpx.Response(404, text="not found")
    )
    mock_editor.set(
        "GET", "/preview/main/unsupported.xyz", httpx.Response(422, text="unsupported")
    )
    client = make_client()

    with pytest.raises(FileNotFoundError, match="main/missing.go"):
        client.render_preview("/main/missing.go")
    with pytest.raises(ValueError, match="main/unsupported.xyz"):
        client.render_preview("main/unsupported.xyz")


def test_render_preview_percent_encodes_resource_path(make_client, mock_editor, tmp_path):
    # MockEditor keys routes on the decoded path; the wire form is checked
    # via raw_path, which preserves the percent-encoding.
    mock_editor.set(
        "GET", "/preview/main/my file&.png", httpx.Response(200, content=PNG_BYTES)
    )
    client = make_client()

    out = client.render_preview("main/my file&.png", out_path=tmp_path / "out.png")
    req = mock_editor.sent("GET", "/preview/main/my file&.png")[0]
    assert req.url.raw_path == b"/preview/main/my%20file%26.png?width=1280&height=720"
    assert out.read_bytes()[:4] == b"\x89PNG"


# -- console -----------------------------------------------------------------


def test_console_get(make_client, mock_editor):
    payload = {"lines": ["[info] started", "[warn] slow"], "regions": []}
    mock_editor.set("GET", "/console", httpx.Response(200, json=payload))
    client = make_client()
    assert client.console() == payload


# -- bob ---------------------------------------------------------------------


def test_bob_call_sends_bearer_and_body(make_client, mock_editor):
    mock_editor.set(
        "POST",
        "/bob",
        [
            httpx.Response(200, json={"ok": True, "output": "built"}),
            httpx.Response(500, json={"error": "bob exploded"}),
        ],
    )
    client = make_client()

    result = client.bob_call(
        {"platform": "wasm-web", "archive": True}, ["resolve", "build", "bundle"]
    )
    assert result == {"ok": True, "output": "built"}

    req = mock_editor.sent("POST", "/bob")[0]
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(req.content) == {
        "options": {"platform": "wasm-web", "archive": True},
        "commands": ["resolve", "build", "bundle"],
    }

    with pytest.raises(EditorClientError) as excinfo:
        client.bob_call({"platform": "wasm-web"}, ["build"])
    assert excinfo.value.status == 500
    assert "bob exploded" in str(excinfo.value)


# -- eval / transact ---------------------------------------------------------


def test_transact_retries_503_three_times(make_client, mock_editor, monkeypatch):
    mock_editor.set(
        "POST",
        "/eval",
        [
            httpx.Response(503, text="initialising"),
            httpx.Response(503, text="initialising"),
            httpx.Response(200, json={"ok": True}),
        ],
    )
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    client = make_client()

    assert client.transact("return 1") == {"ok": True}
    assert sleeps == [0.2, 0.4]
    assert len(mock_editor.sent("POST", "/eval")) == 3

    # Four consecutive 503s exhaust the initial attempt plus 3 retries.
    mock_editor.set(
        "POST",
        "/eval",
        [httpx.Response(503, text="initialising") for _ in range(4)],
    )
    with pytest.raises(EditorClientError) as excinfo:
        client.transact("return 1")
    assert excinfo.value.status == 503
    assert sleeps == [0.2, 0.4, 0.2, 0.4, 0.8]


def test_transact_422_no_retry_lualerror(make_client, mock_editor):
    mock_editor.set(
        "POST", "/eval", httpx.Response(422, text='attempt to index nil (field "x")')
    )
    client = make_client()
    with pytest.raises(LuaScriptError, match="attempt to index nil") as excinfo:
        client.transact("return bad")
    assert excinfo.value.status == 422
    assert len(mock_editor.sent("POST", "/eval")) == 1


def test_transact_non_json_body(make_client, mock_editor):
    mock_editor.set("POST", "/eval", httpx.Response(200, text="plain text result"))
    client = make_client()
    assert client.transact("return 'hi'") == {"ok": True, "result": "plain text result"}


def test_transact_lone_surrogate_raises_value_error(make_client, mock_editor):
    mock_editor.set("POST", "/eval", httpx.Response(200, json={"ok": True}))
    client = make_client()
    with pytest.raises(ValueError, match="UTF-8"):
        client.transact("return '\ud800'")


def test_operations_use_explicit_60s_timeout(make_client, mock_editor):
    mock_editor.set("POST", "/command/compile", httpx.Response(200, json={}))
    mock_editor.set("GET", "/console", httpx.Response(200, json={"lines": [], "regions": []}))
    client = make_client()

    client.compile()
    req = mock_editor.sent("POST", "/command/compile")[0]
    assert req.extensions["timeout"] == {
        "connect": 60.0, "read": 60.0, "write": 60.0, "pool": 60.0
    }

    # A per-call override replaces the default.
    client.console(timeout=2.5)
    req = mock_editor.sent("GET", "/console")[0]
    assert req.extensions["timeout"]["read"] == 2.5


def test_probe_timeout_stays_1_5s(tmp_path):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json=OPENAPI)

    project = make_project(tmp_path)
    client = probe_editor(
        project, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert client is not None
    assert seen["timeout"] == {
        "connect": 1.5, "read": 1.5, "write": 1.5, "pool": 1.5
    }


# -- lua_quote / mutation scripts --------------------------------------------


def _unquote(literal: str) -> str:
    """Decode a double-quoted Lua 5.2 string literal back to its value."""
    assert literal[0] == '"' and literal[-1] == '"'
    body = literal[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt.isdigit():
            out.append(chr(int(body[i + 1 : i + 4], 10)))
            i += 4
        else:
            out.append({"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}[nxt])
            i += 2
    return "".join(out)


def test_lua_quote_injection_safety():
    adversarial = [
        'x")] error("pwned',
        'a\nb',
        'quote " end',
        "back\\slash",
        "\x01\x1b control",
        "]] -- long bracket breakout",
        "tab\there",
        "cr\rhere",
    ]
    for original in adversarial:
        literal = lua_quote(original)
        assert literal.startswith('"') and literal.endswith('"')
        # No raw newlines or unescaped double quotes anywhere in the literal.
        assert "\n" not in literal
        # No unescaped double quotes inside (escaped \" pairs are fine).
        inner = literal[1:-1]
        assert '"' not in inner.replace('\\"', "")
        # Round-trip: decoding the escapes reproduces the original value.
        assert _unquote(literal) == original

    # The generated mutation script must embed the value only as the escaped
    # literal, never as the raw (quote-bearing) string.
    script = editor_client.build_mutation_script(
        [
            {
                "op": "set_property",
                "node": "main",
                "key": "label.text",
                "value": 'x")] error("pwned',
            }
        ],
        "/main/main.collection",
    )
    assert lua_quote('x")] error("pwned') in script
    assert 'x")] error("pwned' not in script
    assert script.rstrip().endswith("editor.save()")


def test_transact_mutations_builds_transact_block(make_client, mock_editor):
    mock_editor.set("POST", "/eval", httpx.Response(200, json={"saved": True}))
    client = make_client()

    result = client.transact_mutations(
        [
            {"op": "add_resource", "path": "/main/new.go", "template": "game_object"},
            {"op": "set_property", "node": "main", "key": "component.property", "value": 42},
        ],
        "/main/main.collection",
    )
    assert result == {"ok": True, "result": {"saved": True}}

    body = mock_editor.sent("POST", "/eval")[0].content.decode("utf-8")
    assert 'editor.resource("/main/new.go")' in body
    assert 'editor.template("game_object")' in body
    assert 'editor.get("main")' in body
    assert 'editor.tx.set(editor.get("main"), "component.property", 42)' in body
    assert body.rstrip().endswith("editor.save()")
    assert mock_editor.sent("POST", "/eval")[0].headers["authorization"] == f"Bearer {TOKEN}"

    with pytest.raises(ValueError, match="Unsupported mutation operation"):
        client.transact_mutations([{"op": "nonsense"}], "/main/main.collection")


# -- token hygiene -----------------------------------------------------------


def test_token_redacted_from_exceptions(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=OPENAPI)
        raise httpx.ConnectError("boom")

    project = make_project(tmp_path)
    client = probe_editor(project, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client is not None

    with pytest.raises(EditorClientError) as excinfo:
        client.transact("return 1")
    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in repr(client)


def test_transport_error_chain_carries_no_token(tmp_path):
    """The chained httpx exception holds the request (with the Bearer
    header); it must not be reachable from the raised exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=OPENAPI)
        raise httpx.ConnectError("boom")

    project = make_project(tmp_path)
    client = probe_editor(
        project, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert client is not None

    with pytest.raises(EditorClientError) as excinfo:
        client.transact("return 1")

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert TOKEN not in str(excinfo.value)


def test_console_non_json_2xx_raises_editor_error(make_client, mock_editor):
    mock_editor.set("GET", "/console", httpx.Response(200, text="not json"))
    client = make_client()
    with pytest.raises(EditorClientError) as excinfo:
        client.console()
    assert excinfo.value.status == 200


def test_bob_call_non_json_2xx_raises_editor_error(make_client, mock_editor):
    mock_editor.set("POST", "/bob", httpx.Response(200, text="not json"))
    client = make_client()
    with pytest.raises(EditorClientError) as excinfo:
        client.bob_call({}, ["build"])
    assert excinfo.value.status == 200


def test_lua_literal_rejects_non_finite_floats():
    import math

    assert editor_client._lua_literal(1.5) == "1.5"
    for bad in (float("inf"), float("-inf"), float("nan")):
        assert not math.isfinite(bad)
        with pytest.raises(TypeError, match="finite"):
            editor_client._lua_literal(bad)
