"""Hermetic tests for the Defold Engine Service client.

HTTP goes through ``httpx.MockTransport``; the TCP log collector is
exercised against a real local asyncio TCP server bound to an ephemeral
loopback port. No real engine, no external network.
"""

import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from defold_mcp import engine_client
from defold_mcp.engine_client import (
    ENGINE_POST_MAX_BYTES,
    EngineClient,
    RecordingClient,
    TcpLogCollector,
    UnsupportedCapability,
    chunk_reload_posts,
    compiled_resource_path,
    encode_reload,
    encode_system_int32,
    encode_system_string,
)

BASE = "http://127.0.0.1:9999"


# -- helpers -----------------------------------------------------------------


def make_client(handler) -> EngineClient:
    transport = httpx.MockTransport(handler)
    return EngineClient(BASE, client=httpx.Client(transport=transport))


def decode_reload(body: bytes) -> list[str]:
    """Decode a repeated string field-1 Protobuf body (test-side parser)."""
    paths = []
    i = 0
    while i < len(body):
        assert body[i] == 0x0A, f"expected tag 0x0a at offset {i}"
        i += 1
        length = 0
        shift = 0
        while True:
            byte = body[i]
            i += 1
            length |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        paths.append(body[i : i + length].decode("utf-8"))
        i += length
    return paths


class LogServer:
    """A real asyncio TCP server on an ephemeral loopback port.

    ``lines`` are the raw byte chunks written to each connecting client,
    in order, before the writer is closed.
    """

    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines
        self.ready = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server: asyncio.Server | None = None
        self.port = 0

    async def _handle(self, reader, writer) -> None:
        for line in self.lines:
            writer.write(line)
        await writer.drain()
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.ready.set()
        try:
            await self.server.serve_forever()
        except asyncio.CancelledError:
            pass  # normal shutdown: stop() cancels serve_forever

    def start(self) -> "LogServer":
        thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)
        thread.start()
        assert self.ready.wait(5), "log server did not start"
        return self

    async def _close(self) -> None:
        assert self.server is not None
        self.server.close()  # synchronous in Python 3.14
        await self.server.wait_closed()

    def stop(self) -> None:
        if self.loop is not None and self.server is not None:
            asyncio.run_coroutine_threadsafe(self._close(), self.loop).result(5)


def wait_for(predicate, timeout: float = 5.0):
    """Poll ``predicate`` until truthy or raise (test-side barrier)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


# -- compiled_resource_path --------------------------------------------------


def test_compiled_resource_path_mapping() -> None:
    cases = {
        "/main/hero.atlas": "/main/hero.texturesetc",
        "/main/tile.tilesource": "/main/tile.texturesetc",
        "/assets/bg.png": "/assets/bg.texturec",
        "/assets/bg.jpg": "/assets/bg.texturec",
        "/assets/bg.tga": "/assets/bg.texturec",
        "/main/player.script": "/main/player.scriptc",
        "/shaders/fx.fp": "/shaders/fx.fpc",
        "/main/gui.gui": "/main/gui.guic",
        "/main/data": "/main/datac",
        # Idempotent: already-compiled paths stay unchanged.
        "/main/player.scriptc": "/main/player.scriptc",
        "/main/hero.texturesetc": "/main/hero.texturesetc",
        "/assets/bg.texturec": "/assets/bg.texturec",
    }
    for source, expected in cases.items():
        assert compiled_resource_path(source) == expected, source
        # Idempotence: mapping an already-compiled path is a no-op.
        # Extensionless paths are excluded: a dotless name has no
        # extension to inspect, so "c" is appended unconditionally.
        if source != "/main/data":
            assert compiled_resource_path(expected) == expected, expected


# -- protobuf encoders -------------------------------------------------------


def test_encode_reload_exact_bytes() -> None:
    assert encode_reload(["/main/a.scriptc"]) == b"\x0a\x0f" + b"/main/a.scriptc"

    two = encode_reload(["/a/b.scriptc", "/c/d.scriptc"])
    assert two == b"\x0a\x0c/a/b.scriptc" + b"\x0a\x0c/c/d.scriptc"

    # A 300-byte path needs a 2-byte varint length: 300 == 0b100101100
    # -> low group 44 | 0x80 == 0xac, high group 2 == 0x02.
    long_path = "/main/" + "x" * 294
    assert len(long_path) == 300
    assert encode_reload([long_path]) == b"\x0a\xac\x02" + long_path.encode("utf-8")


def test_system_messages_wire_bytes() -> None:
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.content)
        return httpx.Response(200)

    client = make_client(handler)
    client.set_update_frequency(60)
    client.set_vsync(1)
    client.exit(0)
    client.reboot("/main")
    client.reboot("a", "b")
    client.toggle_profile()
    client.toggle_physics_debug()

    assert captured == [
        b"\x08\x3c",  # set_update_frequency(60): tag 0x08, varint 60
        b"\x08\x01",  # set_vsync(1)
        b"\x08\x00",  # exit(0)
        b"\x0a\x05/main",  # reboot arg1
        b"\x0a\x01a\x12\x01b",  # reboot arg1, arg2
        b"",  # toggle_profile
        b"",  # toggle_physics_debug
    ]

    with pytest.raises(ValueError, match="six"):
        client.reboot("a", "b", "c", "d", "e", "f", "g")


def test_encode_system_int32_rejects_negatives() -> None:
    # proto2 int32 negatives are 10-byte two's-complement varints, which the
    # encoder deliberately does not emit; the message says so.
    with pytest.raises(ValueError, match="deliberately unsupported"):
        encode_system_int32(1, -1)


def test_chunk_reload_posts_under_1024() -> None:
    paths = [f"/assets/sprites/character_{i:03d}/idle_anim.frame" for i in range(100)]
    chunks = chunk_reload_posts(paths)
    assert len(chunks) > 1  # 100 paths of ~45 bytes cannot fit in one 1024-byte body
    for chunk in chunks:
        assert len(chunk) <= ENGINE_POST_MAX_BYTES

    reassembled: list[str] = []
    for chunk in chunks:
        reassembled.extend(decode_reload(chunk))
    assert reassembled == paths  # order preserved across chunks

    # A single path whose own encoding exceeds the cap is emitted alone.
    huge = "/" + "a" * 2000
    with pytest.raises(ValueError, match="1024"):
        chunk_reload_posts([huge])


# -- EngineClient HTTP -------------------------------------------------------


def test_engine_client_post_binary_enforces_limit() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    client = make_client(handler)
    client.post_binary("@resource", "reload", b"x" * ENGINE_POST_MAX_BYTES)

    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/post/@resource/reload"
    assert seen[0].headers["content-type"] == "application/octet-stream"
    assert len(seen[0].content) == ENGINE_POST_MAX_BYTES

    with pytest.raises(ValueError, match="1024"):
        client.post_binary("@resource", "reload", b"x" * (ENGINE_POST_MAX_BYTES + 1))
    assert len(seen) == 1  # the oversized body never reached the transport


def test_resource_reload_end_to_end_mocked() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    client = make_client(handler)
    results = client.resource_reload(["/main/a.script", "/main/b.atlas"])

    # Both compiled paths fit in one 1024-byte chunk:
    #   "/main/a.scriptc" (15 bytes) -> 0x0a 0x0f
    #   "/main/b.texturesetc" (19 bytes) -> 0x0a 0x13
    assert b"".join(r.content for r in seen) == (
        b"\x0a\x0f/main/a.scriptc" + b"\x0a\x13/main/b.texturesetc"
    )
    assert all(r.url.path == "/post/@resource/reload" for r in seen)
    assert results == [{"posted": 2, "bytes": len(seen[0].content)}]


def test_resource_reload_multi_chunk() -> None:
    # 100 paths of ~45 bytes cannot fit in one 1024-byte body: several chunks.
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    client = make_client(handler)
    paths = [f"/assets/sprites/character_{i:03d}/idle_anim.frame" for i in range(100)]
    results = client.resource_reload(paths)

    assert len(results) > 1  # more than one chunk
    assert len(seen) == len(results)  # one POST per chunk
    assert all(r.url.path == "/post/@resource/reload" for r in seen)
    for request in seen:
        assert len(request.content) <= ENGINE_POST_MAX_BYTES
    # Reassembling the chunks reproduces the compiled paths, in order.
    reassembled: list[str] = []
    for request in seen:
        reassembled.extend(decode_reload(request.content))
    assert reassembled == [compiled_resource_path(p) for p in paths]
    assert sum(r["posted"] for r in results) == len(paths)


def test_resource_reload_chunk_error() -> None:
    # The first chunk POSTs 200, the second 500: resource_reload must raise
    # and report how many earlier chunks already succeeded.
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200) if len(seen) == 1 else httpx.Response(500)

    client = make_client(handler)
    paths = [f"/assets/sprites/character_{i:03d}/idle_anim.frame" for i in range(100)]
    with pytest.raises(RuntimeError) as excinfo:
        client.resource_reload(paths)
    assert excinfo.value.posted_chunks == 1  # chunk 0 succeeded before chunk 1 failed
    assert len(seen) == 2  # the failing chunk was POSTed; no further chunks


def test_discover_engine_port_regex() -> None:
    stdout = (
        "INFO:BOOT: starting engine\n"
        "INFO:ENGINE: Engine service started on port 8123\n"
        "INFO:RENDER: context ready\n"
    )
    assert engine_client.discover_engine_port(stdout) == 8123
    assert engine_client.discover_engine_port("INFO:BOOT: starting engine\n") is None
    assert engine_client.discover_engine_port("") is None


def test_ping_info_state_mocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ping":
            return httpx.Response(200, text="PONG")
        if request.url.path == "/info":
            return httpx.Response(200, json={"version": "1.14.1", "log_port": 8123})
        if request.url.path == "/state":
            return httpx.Response(200, json={"paused": False})
        raise AssertionError(f"unexpected {request.url.path}")

    client = make_client(handler)
    assert client.ping() is True
    assert client.info() == {"version": "1.14.1", "log_port": 8123}
    assert client.state() == {"paused": False}

    def ping_down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="nope")

    assert make_client(ping_down).ping() is False


# -- TcpLogCollector ---------------------------------------------------------


def test_tcp_log_collector_handshake_and_ring() -> None:
    lines = [b"0 OK\n"] + [f"line {i:05d}\n".encode() for i in range(5200)]
    server = LogServer(lines).start()
    try:
        collector = TcpLogCollector("127.0.0.1", server.port)  # capacity 5000
        collector.connect()

        def all_lines_arrived() -> dict | None:
            result = collector.slice(0, 10_000)
            if len(result["lines"]) == 5000 and result["lines"][-1] == "line 05199":
                return result
            return None

        result = wait_for(all_lines_arrived)
        assert result is not None
        # total_seen (5200) exceeds the ring capacity (5000): the ring keeps
        # the LAST 5,000 lines, so the oldest retained line is index 200.
        # next_offset = start + len(window) = max(0, 200) + 5000 = 5200.
        assert result["next_offset"] == 5200
        assert len(result["lines"]) == 5000
        assert result["lines"][0] == "line 00200"
        assert result["lines"][-1] == "line 05199"

        # Offsets stay absolute: a window from a later offset lines up.
        window = collector.slice(5100, 10)
        assert window["lines"] == [f"line {i:05d}" for i in range(5100, 5110)]
        assert window["next_offset"] == 5110

        # Case-insensitive substring filter.
        assert collector.slice(5190, 10, filter="LINE 05195")["lines"] == ["line 05195"]

        # stop() closes the socket and joins the reader thread cleanly.
        thread = collector._thread
        collector.stop()
        assert thread is not None
        assert not thread.is_alive()
    finally:
        server.stop()


def test_tcp_log_collector_bad_handshake() -> None:
    server = LogServer([b"ERROR boom\n"]).start()
    try:
        collector = TcpLogCollector("127.0.0.1", server.port)
        with pytest.raises(ConnectionError, match="handshake"):
            collector.connect()
    finally:
        server.stop()


def test_tcp_log_collector_pagination_no_duplication() -> None:
    # 5,200 lines over a 5,000-line ring: the oldest 200 lines have been
    # evicted. Paginating from offset 0 must not re-emit any line.
    lines = [b"0 OK\n"] + [f"line {i:05d}\n".encode() for i in range(5200)]
    server = LogServer(lines).start()
    try:
        collector = TcpLogCollector("127.0.0.1", server.port)  # capacity 5000
        collector.connect()

        def all_lines_arrived() -> bool:
            # A wide slice advances to the newest line once every line has
            # been received (oldest retained line 200 + 5000 retained = 5200).
            return collector.slice(0, 6000)["next_offset"] >= 5200

        wait_for(all_lines_arrived)

        first = collector.slice(0, 100)
        # The window is clamped to the oldest retained line (index 200), so
        # the first page covers absolute lines 200..299 and advances to 300.
        assert first["lines"][0] == "line 00200"
        assert first["lines"][-1] == "line 00299"
        assert first["next_offset"] == 300

        second = collector.slice(first["next_offset"], 100)
        assert second["lines"][0] == "line 00300"
        assert second["lines"][-1] == "line 00399"

        # The two pages are disjoint: no line is emitted twice, and the
        # second page starts exactly where the first advanced to (no rewind).
        assert not (set(first["lines"]) & set(second["lines"]))
        assert int(second["lines"][0].split()[1]) == first["next_offset"]
        collector.stop()
    finally:
        server.stop()


def test_tcp_log_collector_handshake_timeout() -> None:
    # A server that accepts the connection but never sends the handshake
    # must leave the collector fd-clean: no socket/file behind, stop() a
    # safe no-op, and a fresh collector still able to connect.
    import socket as _socket

    listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    accepted = threading.Event()

    def hold() -> None:
        conn, _ = listener.accept()
        accepted.set()
        try:
            time.sleep(2)  # hold the connection open, never send the handshake
        finally:
            conn.close()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    try:
        collector = TcpLogCollector("127.0.0.1", port)
        with pytest.raises((TimeoutError, OSError)):
            collector.connect(timeout=0.2)
        assert accepted.wait(5)
        # fd-clean: the failed connect left no socket or file behind.
        assert collector._socket is None
        assert collector._file is None
        # stop() on the failed collector is a safe no-op.
        collector.stop()
    finally:
        listener.close()
        thread.join(timeout=5)

    # A second connect() on a NEW collector works.
    server = LogServer([b"0 OK\n", b"line 00000\n"]).start()
    try:
        fresh = TcpLogCollector("127.0.0.1", server.port)
        fresh.connect()
        fresh.stop()
    finally:
        server.stop()


# -- RecordingClient ---------------------------------------------------------


def test_recording_client_full_protocol(tmp_path: Path) -> None:
    payload = b"\x00\x01fake-mp4-bytes" * 10
    receipt_sha = hashlib.sha256(payload).hexdigest()

    state = {"status_polls": 0, "deletes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/recording/capabilities":
            return httpx.Response(200, json={"supported": True, "instance_id": "inst-7"})
        if request.method == "POST" and path == "/recording/start":
            assert request.headers["x-recording-instance"] == "inst-7"
            assert json.loads(request.content) == {"fps": 30, "audio": True}
            return httpx.Response(202, json={"operation_id": "op-1"})
        if request.method == "POST" and path == "/recording/stop":
            assert request.headers["x-recording-instance"] == "inst-7"
            assert json.loads(request.content) == {"operation_id": "op-1"}
            return httpx.Response(200, json={})
        if request.method == "GET" and path == "/recording/status":
            state["status_polls"] += 1
            if state["status_polls"] < 3:
                return httpx.Response(200, json={"pending": True, "finalized": False})
            return httpx.Response(
                200,
                json={
                    "pending": False,
                    "finalized": True,
                    "file": {
                        "id": "f-1",
                        "state": "complete",
                        "size": len(payload),
                        "sha256": receipt_sha,
                    },
                },
            )
        if request.method == "GET" and path == "/recording/files/f-1":
            return httpx.Response(200, content=payload)
        if request.method == "DELETE" and path == "/recording/files/f-1":
            state["deletes"] += 1
            return httpx.Response(200)
        raise AssertionError(f"unexpected {request.method} {path}")

    engine = make_client(handler)
    recorder = RecordingClient(engine)
    out_path = tmp_path / "take.mp4"

    assert recorder.start() == "op-1"
    result = recorder.stop_and_fetch(out_path, poll_interval=0.01, timeout=5.0)

    assert out_path.read_bytes() == payload
    assert result == {
        "path": str(out_path),
        "bytes": len(payload),
        "sha256": receipt_sha,
        "operation_id": "op-1",
    }
    assert state["status_polls"] == 3
    assert state["deletes"] == 1  # DELETE called exactly once

    # A receipt with a wrong sha256 must raise and leave no file behind.
    bad_sha = "0" * 64
    state["status_polls"] = 0
    state["deletes"] = 0

    def handler_bad_sha(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/recording/status":
            return httpx.Response(
                200,
                json={
                    "pending": False,
                    "finalized": True,
                    "file": {
                        "id": "f-1",
                        "state": "complete",
                        "size": len(payload),
                        "sha256": bad_sha,
                    },
                },
            )
        if request.url.path == "/recording/files/f-1":
            return httpx.Response(200, content=payload)
        if request.url.path == "/recording/capabilities":
            return httpx.Response(200, json={"supported": True, "instance_id": "inst-7"})
        if request.url.path == "/recording/start":
            return httpx.Response(202, json={"operation_id": "op-1"})
        if request.url.path == "/recording/stop":
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    engine_bad = make_client(handler_bad_sha)
    recorder_bad = RecordingClient(engine_bad)
    recorder_bad.start()
    with pytest.raises(ValueError, match="sha256") as mismatch:
        recorder_bad.stop_and_fetch(tmp_path / "bad.mp4", poll_interval=0.01, timeout=5.0)
    assert not (tmp_path / "bad.mp4").exists()
    assert state["deletes"] == 0  # no DELETE when verification fails
    # The orphaned engine-side file is handed back for later cleanup.
    assert mismatch.value.file_id == "f-1"
    assert "f-1" in str(mismatch.value)


def test_recording_client_unsupported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"supported": False})

    recorder = RecordingClient(make_client(handler))
    with pytest.raises(UnsupportedCapability):
        recorder.start()


def test_recording_stop_500_raises_without_polling() -> None:
    # A stop answered 500 must raise before the poll loop: no status polls,
    # no download, no DELETE (a stale "complete" receipt must not be used).
    state = {"stops": 0, "status_polls": 0, "deletes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/recording/capabilities":
            return httpx.Response(200, json={"supported": True, "instance_id": "inst-7"})
        if request.method == "POST" and path == "/recording/start":
            return httpx.Response(202, json={"operation_id": "op-1"})
        if request.method == "POST" and path == "/recording/stop":
            state["stops"] += 1
            return httpx.Response(500, json={"error": "stop failed"})
        if request.method == "GET" and path == "/recording/status":
            state["status_polls"] += 1
            return httpx.Response(
                200,
                json={
                    "pending": False,
                    "finalized": True,
                    "file": {"id": "stale", "state": "complete", "size": 0, "sha256": "0" * 64},
                },
            )
        if request.method == "DELETE":
            state["deletes"] += 1
            return httpx.Response(200)
        raise AssertionError(f"unexpected {request.method} {path}")

    recorder = RecordingClient(make_client(handler))
    recorder.start()
    with pytest.raises(httpx.HTTPStatusError):
        recorder.stop_and_fetch("/tmp/never.mp4", poll_interval=0.01, timeout=5.0)
    assert state["stops"] == 1
    assert state["status_polls"] == 0  # no polling after a failed stop
    assert state["deletes"] == 0


def test_recording_capabilities_without_instance_id() -> None:
    # A 200 capabilities body missing instance_id must raise the client's own
    # error (ValueError), not a raw TypeError from the str-typed header.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"supported": True})

    recorder = RecordingClient(make_client(handler))
    with pytest.raises(ValueError, match="instance_id"):
        recorder.start()
