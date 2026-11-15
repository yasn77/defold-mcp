"""HTTP client for the Defold Editor's local project API.

When the Defold Editor has a project open it serves an HTTP API on
``127.0.0.1``. The port and bearer token are discovered from
``<project_root>/.internal/editor.port`` and
``<project_root>/.internal/editor.token``.

Security model
--------------
* The bearer token is read from ``.internal/editor.token`` inside the
  client, attached only to ``/eval`` and ``/bob`` requests, and never
  appears in exceptions, logs, or returned structures (all exception
  messages are redacted; ``__repr__`` omits it; transport errors are
  raised without an exception chain, so the httpx request — whose headers
  carry the token — is never reachable from a raised error).
* Raw arbitrary ``/eval`` is not exposed to callers of this module's
  high-level API; mutations go through :func:`EditorClient.transact_mutations`,
  which generates parameterised Lua scripts. Every user-supplied string is
  embedded via :func:`lua_quote`, making Lua code injection impossible.

Lua API surface
---------------
The generated mutation scripts use the plan's §2.4.B contract:
``editor.get``, ``editor.tx.add``, ``editor.tx.set``, ``editor.save``.
This surface cannot be verified on a host without a live Editor and MUST
be validated against a running Defold Editor in the live phase.
"""

import math
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from defold_mcp import config

_EDITOR_HOST = "127.0.0.1"
_PORT_FILE = Path(".internal") / "editor.port"
_TOKEN_FILE = Path(".internal") / "editor.token"

# Backoff base for /eval 503 retries (0.2s, 0.4s, 0.8s).
_EVAL_RETRY_BASE_SECONDS = 0.2

# Default timeout for Editor operations (compiles, commands, previews,
# /eval, /bob). The discovery probe keeps its own short 1.5s timeout.
_OPERATION_TIMEOUT_SECONDS = 60.0

# PNG magic bytes, first four octets.
_PNG_MAGIC = b"\x89PNG"

# Characters allowed verbatim in a cached preview file name.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


class EditorClientError(Exception):
    """Raised for Editor API failures (transport errors or non-2xx responses)."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class LuaScriptError(EditorClientError):
    """Raised when ``/eval`` rejects a script with HTTP 422 (a Lua error).

    Never retried: a 422 means the payload itself is faulty.
    """


def lua_quote(value: str) -> str:
    """Render ``value`` as a single double-quoted Lua 5.2 string literal.

    Escapes ``\\``, ``"``, ``\\n``, ``\\r``, ``\\t`` and any other control
    character as ``\\ddd`` (Lua 5.2 numeric escape). The result is a valid
    literal for any input, including adversarial strings containing quotes,
    newlines, or ``)]``-style breakouts: the only unescaped ``"`` characters
    are the two delimiters.
    """
    parts: list[str] = ['"']
    for ch in value:
        code = ord(ch)
        if ch == "\\":
            parts.append("\\\\")
        elif ch == '"':
            parts.append('\\"')
        elif ch == "\n":
            parts.append("\\n")
        elif ch == "\r":
            parts.append("\\r")
        elif ch == "\t":
            parts.append("\\t")
        elif code < 0x20:
            parts.append(f"\\{code:03d}")
        else:
            parts.append(ch)
    parts.append('"')
    return "".join(part for part in parts)


def _lua_literal(value: Any) -> str:
    """Render a Python value as a Lua 5.2 literal.

    Non-finite floats (``inf``, ``-inf``, ``nan``) are rejected with
    :class:`TypeError`: Lua 5.2 has no such literals and ``repr`` would
    emit a token the Editor's Lua parser cannot read.
    """
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise TypeError(
                f"Non-finite float cannot be rendered as a Lua literal: {value!r}"
            )
        return repr(value)
    if isinstance(value, str):
        return lua_quote(value)
    if isinstance(value, (list, tuple)):
        return "{" + ", ".join(_lua_literal(item) for item in value) + "}"
    raise TypeError(f"Cannot render {type(value).__name__} as a Lua literal")


# -- mutation script generation (table-driven; extend by adding builders) ----


def _op_add_resource(op: dict) -> str:
    path = op.get("path")
    template = op.get("template")
    if not isinstance(path, str) or not isinstance(template, str):
        raise ValueError("add_resource requires string 'path' and 'template'")
    return (
        f"editor.tx.add(editor.resource({lua_quote(path)}), "
        f"editor.template({lua_quote(template)}))"
    )


def _op_set_property(op: dict) -> str:
    node = op.get("node")
    key = op.get("key")
    if not isinstance(node, str) or not isinstance(key, str):
        raise ValueError("set_property requires string 'node' and 'key'")
    value = op.get("value")
    return f"editor.tx.set(editor.get({lua_quote(node)}), {lua_quote(key)}, {_lua_literal(value)})"


_OP_BUILDERS: dict[str, Any] = {
    "add_resource": _op_add_resource,
    "set_property": _op_set_property,
}


def build_mutation_script(operations: list[dict], resource_path: str) -> str:
    """Build the Lua 5.2 script applying ``operations`` in one transaction.

    Shape (plan §2.4.B): a single ``editor.transact({ ... })`` wrapping the
    generated ``editor.tx.add`` / ``editor.tx.set`` statements, followed by
    ``editor.save()`` as the final statement. ``node`` values are node path
    strings; the client itself generates the ``editor.get(...)`` expression
    from the quoted path, so no caller-supplied text ever reaches the
    script unquoted.
    """
    statements: list[str] = []
    for op in operations:
        name = op.get("op") if isinstance(op, dict) else None
        builder = _OP_BUILDERS.get(name) if isinstance(name, str) else None
        if builder is None:
            raise ValueError(f"Unsupported mutation operation: {name!r}")
        statements.append(builder(op))
    lines = ["-- mutations for " + lua_quote(resource_path), "editor.transact({"]
    lines.extend(f"    {statement}," for statement in statements)
    lines.append("})")
    lines.append("editor.save()")
    return "\n".join(lines)


# -- issue conversion ---------------------------------------------------------


def _as_one_based(value: Any) -> int | None:
    """A 0-based Editor range value converted to 1-based, or ``None``.

    Malformed values (non-integers, booleans, missing) yield ``None``
    rather than raising: a bad range must not break issue reporting.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value + 1
    return None


def _convert_issue(issue: dict) -> dict:
    """Convert an Editor compile issue to 1-based line/col diagnostics.

    The assumed 0-based range shape is::

        {"start": {"line": L0, "character": C0}, "end": {"line": E0, "character": E0}}

    Malformed range fields (non-integers, booleans, missing) yield
    ``None`` rather than raising.
    """
    out: dict[str, Any] = {
        "message": issue.get("message"),
        "severity": issue.get("severity"),
        "resource": issue.get("resource"),
        "line": None,
        "col": None,
        "end_line": None,
        "end_col": None,
    }
    rng = issue.get("range")
    if isinstance(rng, dict):
        start = rng.get("start") if isinstance(rng.get("start"), dict) else {}
        end = rng.get("end") if isinstance(rng.get("end"), dict) else {}
        out["line"] = _as_one_based(start.get("line"))
        out["col"] = _as_one_based(start.get("character"))
        out["end_line"] = _as_one_based(end.get("line"))
        out["end_col"] = _as_one_based(end.get("character"))
    return out


def _body_summary(response: httpx.Response) -> str:
    """A short, safe summary of a response body for error messages."""
    try:
        return response.text[:200]
    except Exception:  # pragma: no cover - body already consumed
        return "<unreadable body>"


# -- client -------------------------------------------------------------------


class EditorClient:
    """A bound client for one running Defold Editor instance.

    Construct via :func:`probe_editor`; direct construction is supported
    but not required (tests inject ``httpx.Client`` through the probe).
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        spec: dict | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._spec = spec or {}
        self._client = client

    # -- properties ----------------------------------------------------------

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def spec_version(self) -> str | None:
        info = self._spec.get("info")
        return info.get("version") if isinstance(info, dict) else None

    @property
    def capabilities(self) -> frozenset[str]:
        """Negotiated route set from the OpenAPI spec, as ``"METHOD /path"``."""
        routes: set[str] = set()
        for path, item in (self._spec.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            for method in item:
                routes.add(f"{method.upper()} {path}")
        return frozenset(routes)

    def has_route(self, method: str, path: str) -> bool:
        """True when the fetched OpenAPI spec declares this route."""
        return f"{method.upper()} {path}" in self.capabilities

    def __repr__(self) -> str:  # token must never appear here
        return f"<EditorClient {self._base_url} spec={self.spec_version}>"

    # -- internals -----------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Bearer headers for ``/eval`` and ``/bob`` only."""
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    def _redact(self, text: str) -> str:
        """Remove the bearer token from any text before it leaves the client."""
        if self._token and self._token in text:
            return text.replace(self._token, "[redacted]")
        return text

    def _request(
        self,
        method: str,
        path: str,
        *,
        client: httpx.Client | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Issue a request; transport errors become redacted EditorClientError.

        The raised error carries NO exception chain: the httpx exception
        holds the request whose headers include the bearer token, so it
        must not be reachable via ``__cause__`` or ``__context__``. The
        error is therefore raised outside the ``except`` block.
        """
        own = client is None and self._client is None
        http = client if client is not None else (self._client or httpx.Client())
        transport_error: httpx.HTTPError | None = None
        response: httpx.Response | None = None
        try:
            response = http.request(method, self._base_url + path, **kwargs)
        except httpx.HTTPError as exc:
            transport_error = exc
        finally:
            if own:
                http.close()
        if transport_error is not None:
            raise EditorClientError(
                self._redact(f"Editor request {method} {path} failed: {transport_error}")
            ) from None
        assert response is not None
        return response

    def _error_for(self, method: str, path: str, response: httpx.Response) -> EditorClientError:
        body = self._redact(_body_summary(response))
        detail = ""
        try:
            data = response.json()
            if isinstance(data, dict):
                for key in ("error", "message"):
                    if isinstance(data.get(key), str):
                        detail = data[key]
                        break
        except ValueError:
            pass
        message = f"Editor {method} {path} failed with HTTP {response.status_code}"
        if detail:
            message += f": {self._redact(detail)}"
        elif body:
            message += f": {body}"
        return EditorClientError(message, status=response.status_code, body=body)

    # -- probe ----------------------------------------------------------------

    # -- compile ---------------------------------------------------------------

    def compile(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = _OPERATION_TIMEOUT_SECONDS,
    ) -> dict:
        """POST /command/compile.

        200 → ``{"ok": True, "issues": []}``; 422 → ``{"ok": False,
        "issues": [...]}`` with 0-based range values converted to 1-based
        ``line``/``col``/``end_line``/``end_col`` (malformed range fields
        become ``None``); a 422 whose body is not a JSON object raises
        :class:`EditorClientError`; other statuses raise
        :class:`EditorClientError`.
        """
        response = self._request(
            "POST", "/command/compile", client=client, timeout=timeout
        )
        if response.status_code == 200:
            return {"ok": True, "issues": []}
        if response.status_code == 422:
            try:
                body: Any = response.json()
            except ValueError:
                body = None
            if not isinstance(body, dict):
                raise self._error_for("POST", "/command/compile", response)
            issues = [
                _convert_issue(issue)
                for issue in body.get("issues", [])
                if isinstance(issue, dict)
            ]
            return {"ok": False, "issues": issues}
        raise self._error_for("POST", "/command/compile", response)

    # -- commands --------------------------------------------------------------

    def _post_command(
        self,
        path: str,
        *,
        client: httpx.Client | None = None,
        params: dict[str, str] | None = None,
        timeout: float = _OPERATION_TIMEOUT_SECONDS,
    ) -> dict:
        response = self._request(
            "POST", path, client=client, params=params, timeout=timeout
        )
        if 200 <= response.status_code < 300:
            result: dict[str, Any] = {"ok": True}
            if response.text:
                try:
                    body = response.json()
                    if isinstance(body, dict):
                        result.update(body)
                except ValueError:
                    pass
            return result
        raise self._error_for("POST", path, response)

    def run(
        self,
        focus: bool = False,
        *,
        client: httpx.Client | None = None,
        timeout: float = _OPERATION_TIMEOUT_SECONDS,
    ) -> dict:
        """POST /command/run?focus=... (compile + launch debug engine)."""
        return self._post_command(
            "/command/run",
            client=client,
            params={"focus": "true" if focus else "false"},
            timeout=timeout,
        )

    def hot_reload(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = _OPERATION_TIMEOUT_SECONDS,
    ) -> dict:
        """POST /command/hot-reload."""
        return self._post_command(
            "/command/hot-reload", client=client, timeout=timeout
        )

    def fetch_libraries(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = _OPERATION_TIMEOUT_SECONDS,
    ) -> dict:
        """POST /command/fetch-libraries."""
        return self._post_command(
            "/command/fetch-libraries", client=client, timeout=timeout
        )

    def clean_build(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = _OPERATION_TIMEOUT_SECONDS,
    ) -> dict:
        """POST /command/clean-build."""
        return self._post_command("/command/clean-build", client=client, timeout=timeout)

    def build_html5(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = _OPERATION_TIMEOUT_SECONDS,
    ) -> dict:
        """POST /command/build-html5; the result carries the served-at URL."""
        return self._post_command(
            "/command/build-html5", client=client, timeout=timeout
        )

    def reload_extensions(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = _OPERATION_TIMEOUT_SECONDS,
    ) -> dict:
        """POST /command/reload-extensions."""
        return self._post_command(
            "/command/reload-extensions", client=client, timeout=timeout
        )

    # -- preview ----------------------------------------------------------------

    def render_preview(
        self,
        resource_path: str,
        width: int = 1280,
        height: int = 720,
        out_path: Path | None = None,
        *,
        client: httpx.Client | None = None,
        timeout: float = _OPERATION_TIMEOUT_SECONDS,
    ) -> Path:
        """GET /preview/{path} and write the PNG atomically to ``out_path``.

        The resource path is percent-encoded (``/`` kept as the path
        separator) so names with spaces, ``&``, or other reserved
        characters reach the Editor intact.

        Default location: ``<cache>/previews/<sanitised-name>-<w>x<h>.png``.
        404 → :class:`FileNotFoundError`; 422 → :class:`ValueError`;
        non-PNG bytes → :class:`ValueError`.
        """
        rel = urllib.parse.quote(resource_path.lstrip("/"), safe="/")
        response = self._request(
            "GET",
            f"/preview/{rel}",
            client=client,
            params={"width": str(width), "height": str(height)},
            timeout=timeout,
        )
        if response.status_code == 404:
            raise FileNotFoundError(f"Preview resource not found: {resource_path}")
        if response.status_code == 422:
            raise ValueError(f"Unsupported preview resource type: {resource_path}")
        if not 200 <= response.status_code < 300:
            raise self._error_for("GET", f"/preview/{rel}", response)
        content = response.content
        if not content.startswith(_PNG_MAGIC):
            raise ValueError(f"Preview response for {resource_path} is not a PNG")
        if out_path is None:
            name = _SAFE_NAME_RE.sub("-", Path(resource_path).name)
            out_path = config.get_cache_dir() / "previews" / f"{name}-{width}x{height}.png"
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(out_path.name + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, out_path)
        return out_path

    # -- console -----------------------------------------------------------------

    def console(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = _OPERATION_TIMEOUT_SECONDS,
    ) -> dict:
        """GET /console → parsed ``{"lines": [...], "regions": [...]}``.

        A 2xx response whose body is not a JSON object raises
        :class:`EditorClientError` rather than leaking a raw parse error.
        """
        response = self._request("GET", "/console", client=client, timeout=timeout)
        if 200 <= response.status_code < 300:
            try:
                data: Any = response.json()
            except ValueError:
                data = None
            if not isinstance(data, dict):
                raise self._error_for("GET", "/console", response)
            return data
        raise self._error_for("GET", "/console", response)

    # -- bob -----------------------------------------------------------------------

    def bob_call(
        self,
        options: dict,
        commands: list[str],
        *,
        client: httpx.Client | None = None,
        timeout: float = _OPERATION_TIMEOUT_SECONDS,
    ) -> dict:
        """POST /bob with bearer auth; 4xx/5xx raise :class:`EditorClientError`.

        A 2xx response whose body is not a JSON object raises
        :class:`EditorClientError` rather than leaking a raw parse error.
        """
        response = self._request(
            "POST",
            "/bob",
            client=client,
            headers=self._auth_headers(),
            json={"options": options, "commands": commands},
            timeout=timeout,
        )
        if 200 <= response.status_code < 300:
            try:
                data: Any = response.json()
            except ValueError:
                data = None
            if not isinstance(data, dict):
                raise self._error_for("POST", "/bob", response)
            return data
        raise self._error_for("POST", "/bob", response)

    # -- eval / mutations -----------------------------------------------------------

    def transact(
        self,
        lua_script: str,
        *,
        client: httpx.Client | None = None,
        retries: int = 3,
        timeout: float = _OPERATION_TIMEOUT_SECONDS,
    ) -> dict:
        """POST /eval (bearer auth, ``Content-Type: text/plain``).

        503 → the extension VM is still initialising: retry up to
        ``retries`` times with exponential backoff (0.2s, 0.4s, 0.8s).
        422 → a Lua error in the payload: raise :class:`LuaScriptError`
        carrying the Editor's message, never retried.
        200 → the parsed JSON body, or ``{"ok": True, "result": <text>}``
        when the body is not JSON.

        A script that is not valid UTF-8 (e.g. contains a lone surrogate)
        raises :class:`ValueError` before any request is made.
        """
        try:
            payload = lua_script.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"Lua script is not valid UTF-8: {exc}") from exc
        delays = [_EVAL_RETRY_BASE_SECONDS * (2**attempt) for attempt in range(retries)]
        attempt = 0
        while True:
            response = self._request(
                "POST",
                "/eval",
                client=client,
                headers={**self._auth_headers(), "Content-Type": "text/plain"},
                content=payload,
                timeout=timeout,
            )
            if response.status_code == 503:
                if attempt < len(delays):
                    time.sleep(delays[attempt])
                    attempt += 1
                    continue
                raise EditorClientError(
                    self._redact(
                        f"Editor /eval still returning 503 after {len(delays)} retries"
                    ),
                    status=503,
                )
            if response.status_code == 422:
                raise LuaScriptError(
                    self._redact(
                        f"Editor rejected the Lua script: {response.text}"
                    ),
                    status=422,
                    body=self._redact(_body_summary(response)),
                )
            if 200 <= response.status_code < 300:
                try:
                    return response.json()
                except ValueError:
                    return {"ok": True, "result": response.text}
            raise self._error_for("POST", "/eval", response)

    def transact_mutations(
        self,
        operations: list[dict],
        resource_path: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = _OPERATION_TIMEOUT_SECONDS,
    ) -> dict:
        """Apply ``operations`` to ``resource_path`` via a generated script.

        Builds the plan's §2.4.B shape — a single ``editor.transact`` block
        of ``editor.tx.add`` / ``editor.tx.set`` statements followed by
        ``editor.save()`` — and sends it through :meth:`transact`. The
        Lua API surface must be validated against a live Editor in the
        live phase.
        """
        script = build_mutation_script(operations, resource_path)
        result = self.transact(script, client=client, timeout=timeout)
        return {"ok": True, "result": result}


# -- discovery -----------------------------------------------------------------


def probe_editor(
    project_root: Path | str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 1.5,
) -> EditorClient | None:
    """Discover a running Editor for ``project_root``.

    Reads ``.internal/editor.port`` (missing/unreadable/non-integer → None)
    and GETs ``http://127.0.0.1:<port>/openapi.json`` with ``timeout``;
    any failure → None (never raises). On 200 returns an
    :class:`EditorClient` bound to the base URL, the token from
    ``.internal/editor.token`` (may be None), and the parsed spec. The
    openapi.json GET doubles as the liveness probe.
    """
    root = Path(project_root)
    try:
        port = int((root / _PORT_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None

    token: str | None = None
    try:
        token = (root / _TOKEN_FILE).read_text(encoding="utf-8").strip() or None
    except OSError:
        token = None

    own = client is None
    http = client if client is not None else httpx.Client()
    try:
        response = http.get(f"http://{_EDITOR_HOST}:{port}/openapi.json", timeout=timeout)
        if response.status_code != 200:
            return None
        spec = response.json()
        if not isinstance(spec, dict):
            return None
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if own:
            http.close()

    return EditorClient(
        f"http://{_EDITOR_HOST}:{port}", token=token, spec=spec, client=client
    )
