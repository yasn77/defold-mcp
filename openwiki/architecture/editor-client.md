---
type: architecture
title: Editor HTTP client
description: The Defold Editor HTTP client — probe-based discovery, OpenAPI negotiation, command/preview routes, the token-isolated /eval bridge with injection-proof Lua quoting, and the /bob route.
tags: [defold, editor, http, lua, security]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T02:39:02.076Z
sources:
  - id: openwiki-source-3c978bb71c1869c84b6a4726
    resource: repo://src/defold_mcp/editor_client.py
  - id: openwiki-source-16e4a1820bcc784d90e2edc0
    resource: repo://tests/test_editor_client.py
generated: { by: "opencode", at: "2026-11-15T02:39:02.076Z" }
---

# Editor HTTP client

`src/defold_mcp/editor_client.py` is the Live-Editor half of the project's dual-mode design: when a Defold Editor has the project open, this client routes commands, previews and structured mutations to it instead of the headless toolchain. The [toolchain runners](bob-and-test-runners.md) cover the headless half.

## Discovery — `probe_editor(project_root)`

Reads `.internal/editor.port` (returns `None` when missing, unreadable or non-integer — including out-of-range values) and probes `GET http://127.0.0.1:{port}/openapi.json` with a **1.5-second timeout**: any failure returns `None`, never raises; a 200 returns a bound `EditorClient` carrying the base URL, the parsed OpenAPI spec (capability negotiation: `spec_version`, `has_route`) and the bearer token read from `.internal/editor.token`. A pooled client with a configured timeout should be injected at wiring time — without injection, every operation opens a fresh client with the module's own defaults.

## Routes (exact, negotiated against the OpenAPI spec)

- `POST /command/compile` — `200 {"success": true, "issues": []}` → `{"ok": True}`; `422 {"success": false, "issues": [...]}` → structured issues with **0-based → 1-based** line/col conversion (`_as_one_based` coerces non-int fields to `None`); malformed bodies (non-dict JSON, non-JSON 200s) degrade to `EditorClientError`, never tracebacks.
- `POST /command/run?focus=false`, `/command/hot-reload`, `/command/fetch-libraries`, `/command/clean-build`, `/command/build-html5` (returns the served-at URL), `/command/reload-extensions`.
- `GET /preview/{path}?width&height` — the resource path is **percent-encoded** before interpolation (`quote(path, safe="/")`, so `?`/`#`/`%` in filenames survive the wire); 200 PNG bytes are magic-verified (`\x89PNG`) and written atomically; 404 → `FileNotFoundError`; 422 → `ValueError`.
- `GET /console` — parsed lines/regions.
- `POST /bob` — `Authorization: Bearer`, JSON options/commands body; the Editor's own Java runs the build.

## The `/eval` bridge and `lua_quote`

`POST /eval` carries Lua 5.2 source (`Content-Type: text/plain`) with bearer auth. `transact()` implements the plan's retry contract: **HTTP 503 (extension VM initialising) is retried up to 3 times with exponential backoff; HTTP 422 is never retried** — it raises `LuaScriptError` carrying the Editor's error message. Non-JSON 200 bodies are tolerated.

`transact_mutations(operations, …)` is the SAFE high-level bridge: a table-driven generator (`_OP_BUILDERS`) emits `editor.transact({editor.tx.add(...), editor.tx.set(...)})` followed by `editor.save()`. Every user-supplied value is embedded through:

- `lua_quote(s)` — double-quoted Lua string literals with full escaping (`\`, `"`, newlines, control chars as `\ddd`, NULs) — injection-proof for adversarial input; and
- `_lua_literal(v)` — numbers/booleans/strings rendered as Lua literals, with **non-finite floats rejected** (`inf`/`nan` would evaluate as Lua's undefined globals → `nil`).

Nodes are addressed as **path strings** which the client itself wraps in `editor.get(lua_quote(path))` — no raw Lua expressions from callers at all. Scripts are UTF-8-encoded up front; unencodable content (lone surrogates) raises `ValueError` before any request. Two ops are implemented now (`set_property`, `add_resource`); unknown ops raise `ValueError`; the op table is the extension point for the wiring task. **The Lua API surface (`editor.get`/`tx` signatures) and the 422 range shape are plan assumptions pending live validation.**

## Token hygiene

The bearer token is read from `.internal/editor.token` inside the client and attached **only** to `/eval` and `/bob` requests. It never appears in exception messages, bodies, `repr`, or the exception chain — HTTP errors are caught, the request is closed, and `EditorClientError` is raised **outside** the `except` block with `from None`, so `__cause__`/`__context__` cannot leak the original httpx request object (whose headers would carry the token). Operations use an explicit 60-second timeout (the probe keeps 1.5s); per-call overrides are accepted.
