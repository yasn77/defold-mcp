---
type: architecture
title: Engine service client
description: The running-engine client — binary Protobuf wire encoding for hot-reload and system commands, the TCP log collector with eviction-safe offsets, and the MP4 recording protocol.
tags: [defold, engine, protobuf, tcp, recording]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T04:08:33.115Z
sources:
  - id: openwiki-source-9c7f7467cc94676edbe8c203
    resource: repo://src/defold_mcp/engine_client.py
  - id: openwiki-source-b70833c89ec665a468448695
    resource: repo://tests/test_engine_protobuf.py
generated: { by: "opencode", at: "2026-11-15T04:08:33.115Z" }
---

# Engine service client

`src/defold_mcp/engine_client.py` speaks to a running debug `dmengine`'s HTTP/TCP service: the core routes, the binary Protobuf hot-reload wire format, the TCP log stream, and the native MP4 recorder. The port is discovered by scanning engine stdout for `INFO:ENGINE: Engine service started on port (\d+)` (fallback `DEFAULT_ENGINE_PORT = 8001` for standalone launches).

## Binary Protobuf wire format

`POST /post/<socket>/<message>` with `Content-Type: application/octet-stream`, bodies capped at `ENGINE_POST_MAX_BYTES = 1024`. Proto2 encoding: varint tag `(field << 3) | wire_type` (0 = varint int32, 2 = length-delimited UTF-8).

- `compiled_resource_path()` maps sources to compiled artifacts: `.atlas`/`.tilesource` → `.texturesetc`; `.png`/`.jpg`/`.jpeg`/`.tga` → `.texturec`; every other source extension (and extensionless paths) appends `c`; already-compiled paths stay unchanged.
- `encode_reload(paths)` — `dmResourceDDF.Reload`'s repeated `string resources = 1`: `encode_reload(["/main/a.scriptc"]) == b"\x0a\x0f/main/a.scriptc"`.
- `@system` messages: `set_update_frequency(f)` / `set_vsync(n)` / `exit(code)` — tag `0x08` + varint (negatives rejected: proto2 int32 negatives are 10-byte two's-complement varints, deliberately unsupported); `reboot(*args)` — up to six string args on tags `0x0a`…`0x32`; `toggle_profile`/`toggle_physics_debug` — empty bodies.
- Chunking is a single primitive (`_chunk_paths`): greedy batches under the 1024-byte cap with order preservation; `chunk_reload_posts` is a thin wrapper; a lone path that cannot fit raises. `resource_reload` maps, chunks and POSTs — raising on any non-2xx chunk with the count of already-posted chunks attached (`posted_chunks`).

## `TcpLogCollector`

Connects to the engine's `log_port`, verifies the first line starts with `"0"` (the `"0 OK"` handshake — anything else raises `ConnectionError`; a read timeout self-cleans via the idempotent `_teardown()`), discards it, then a daemon thread buffers lines into a 5,000-entry ring.

**Offset semantics (the subtle part):** `slice(offset, limit, filter)` uses ABSOLUTE line counters that survive ring eviction: when `offset` predates the oldest retained line, the start clamps up to the oldest, and `next_offset = start + len(window)` — so paged reads (read → read at `next_offset`) are always disjoint and monotone, never duplicated (a review probe walked 50 disjoint pages through a 5,300-line eviction scenario). `filter` matches substrings without advancing offsets; `stop()` joins the reader thread and is safe to call twice.

## `RecordingClient`

The four-step MP4 protocol (Defold 1.14.1+ desktop debug engines): capabilities (require `supported` + capture `instance_id`), `POST /recording/start` with the `X-Recording-Instance` header (202 → `operation_id`), `POST /recording/stop` with the same header — **the stop response is checked before any polling** (a failed stop must never fall through to the engine-global `/recording/status`, which can hold a stale receipt) — then poll status until `pending == false and finalized == true and file.state == "complete"`, verify byte length AND SHA-256 against the receipt, save atomically, and `DELETE /recording/files/{id}`. Verification failures raise `ValueError` carrying the orphaned `file_id` so a later cleanup can still DELETE it. The client is **stateful** (`_operation_id`) — never cached across tool calls.

## Where the behaviour is pinned

`tests/test_engine_protobuf.py` (25 tests): exact wire-byte vectors, chunker boundary and reassembly, a real local asyncio TCP server driving the handshake/ring/eviction/pagination scenarios, and the stateful mocked recording protocol (stop-500 → zero polls; mismatch → file_id; DELETE-exactly-once).
