---
type: architecture
title: Automation bridge client
description: The extension-automation-bridge v2 client — DLIB 500 unwrapping into structured errors, FIFO lease auto-renewal, guard-carrying input actions and screenshot capture with sha256 validation.
tags: [defold, automation, bridge, testing, screenshots]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T04:08:33.115Z
sources:
  - id: openwiki-source-da0424a3f86dd91d4c4987ee
    resource: repo://src/defold_mcp/bridge_client.py
  - id: openwiki-source-3ff7724e8a892710545e18d3
    resource: repo://tests/test_bridge_v2.py
generated: { by: "opencode", at: "2026-11-15T04:08:33.115Z" }
---

# Automation bridge client

`src/defold_mcp/bridge_client.py` drives the community `defold/extension-automation-bridge` v2 — the extension that adds live-scene inspection, input injection and post-render screenshots to a running debug engine, under `http://127.0.0.1:<engine_port>/automation-bridge/v2`. Together with the [engine client](engine-client.md) it forms the live-runtime half of the server.

## The DLIB status-mapping quirk

Defold's embedded C++ web server has reason phrases only for 200/302/404/500, so the extension reports logical errors as **HTTP 500** carrying `{"ok": false, "error": {"status": 409, "code": "stale_scene", "message": "…"}}`. The client's `_request` wrapper always parses the JSON: a body with `ok is False` and an `error` dict raises `BridgeApiError` carrying the LOGICAL `status`, `code` and `message` (never the HTTP confusion); raw or non-JSON errors degrade to a raw-status `BridgeApiError`; 2xx responses return parsed JSON (non-JSON 2xx tolerated as `{"ok": True, "raw": …}`). A 200 carrying `ok: false` correctly does NOT raise — success is the HTTP status, `ok` only enriches errors.

## Inspection

`health()` (capabilities, scene_sequence, engine_frame, screen, input devices), `scene(visible, include)` and `elements(type/name/text/automation_id/visible/limit)` — the element search tolerating both `{"elements": […]}` and bare-list response shapes, with `[]` for a dict without the key. Elements carry `id`, `logical_id`, `scene_sequence` and window-pixel `bounds.center`.

## FIFO input lease

Mutating input requires a lease: `PUT /input/configure?client_id=defold-mcp&session_id=<uuid>&lease=10&device=auto&visualize=1`. The session UUID is lazy-generated then sticky per client instance; expiry is tracked on the `time.monotonic()` clock, and every input action re-checks validity and silently re-configures (same session UUID) when expired. `release_lease()` never raises — releasing without a prior lease returns a warning dict.

## Input actions

`click`, `drag` (`from`/`to`), `drag_path` (`points`), `key` (`action`+`key`), `wheel` (`delta_x`/`delta_y`) share one flow: resolve the target (a direct element dict, an `automation_id` lookup taking the first match — `LookupError` naming it when none — or coordinates), POST the JSON body carrying the element `id` plus the guards `expected_scene_sequence` and `expected_logical_id`, then poll `GET /input/status?input_id=…` every 0.05 s until `state == "released"`. Timeouts raise `TimeoutError` with the last status attached (`last_status`), so tool wrappers can report what stuck. Malformed 2xx bodies (missing `input_id`, element without `id`) raise client-owned errors naming the missing key — never `KeyError`/`TypeError`.

## Screenshots

`take_screenshot(after_frames=1)`: `GET /screenshot` returns a `capture_id`; the client polls `GET /screenshot/status` until `state == "complete"`, then validates the receipt's `sha256` against the file at the engine-provided `path` (loopback-only: engine and server share a host). Mismatch → `BridgeApiError(code="sha256_mismatch")`; the result reports `"verified": sha256 is not None` — an absent hash is reported honestly, never as verified.

## Task-11 wiring notes

The lease is instance state — wiring must decide per-call `release_lease()` vs a session-lifetime hold; the engine-side screenshot path couples the tool to the engine's host; production polls use real `time.sleep(0.05)`, so tool wrappers must pass explicit `poll_timeout`s. Wire formats are plan-verified and need one live probe before shipping tools.
