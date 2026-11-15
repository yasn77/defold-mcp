"""Client for the Defold Engine Service (a running debug ``dmengine``).

A debug engine exposes an HTTP service on ``127.0.0.1`` (port discovered
from the engine's stdout — see :func:`discover_engine_port` — or
``DEFAULT_ENGINE_PORT`` for a standalone ``dmengine``) plus a TCP log
stream on the ``log_port`` reported by ``GET /info``.

Binary control messages go to ``POST /post/<socket>/<message>`` as
Proto2 ``application/octet-stream`` bodies of at most
``ENGINE_POST_MAX_BYTES`` (1024) bytes:

- ``@resource/reload`` — ``repeated string resources = 1``
- ``@system/set_update_frequency`` / ``set_vsync`` / ``exit`` —
  ``int32`` field 1
- ``@system/reboot`` — ``optional string arg1..arg6 = 1..6``
- ``@system/toggle_profile`` / ``toggle_physics_debug`` — empty body

Native MP4 recording (Defold 1.14.1+ desktop debug engines) is driven by
:class:`RecordingClient` over the ``/recording/*`` JSON routes.
"""

import collections
import hashlib
import os
import re
import socket
import tempfile
import threading
import time
from pathlib import Path

import httpx

from defold_mcp.config import DEFAULT_ENGINE_PORT, ENGINE_POST_MAX_BYTES

# Regex over engine stdout for the Engine Service port announcement.
# Standalone ``dmengine`` runs without this line fall back to
# ``DEFAULT_ENGINE_PORT`` (8001).
ENGINE_PORT_PATTERN = re.compile(r"INFO:ENGINE: Engine service started on port (\d+)")

# Source extensions that compile to the shared texture-set artifact.
_TEXTURE_SOURCE_EXTS = frozenset({"atlas", "tilesource"})
# Source extensions that compile to a single-texture artifact.
_IMAGE_SOURCE_EXTS = frozenset({"png", "jpg", "jpeg", "tga"})
# Compiled texture forms (already-compiled paths stay unchanged).
_COMPILED_TEXTURE_EXTS = frozenset({"texturesetc", "texturec"})


def compiled_resource_path(path: str) -> str:
    """Map a source resource path to its compiled (``c``-suffixed) form.

    Mapping rules (idempotent for extension-bearing paths — an
    already-compiled path is unchanged):

    - ``.atlas`` / ``.tilesource`` -> ``.texturesetc``
    - ``.png`` / ``.jpg`` / ``.jpeg`` / ``.tga`` -> ``.texturec``
    - any other extension -> ``c`` appended (``.script`` -> ``.scriptc``,
      ``.vp`` -> ``.vpc``, ``.fp`` -> ``.fpc``, ``.gui`` -> ``.guic``, ...)
    - a path with no extension also gets ``c`` appended (``/main/data``
      -> ``/main/datac``). This rule is not idempotent: a dotless name
      has no extension to inspect, so ``/main/datac`` -> ``/main/datacc``.

    An extension is treated as already compiled when it is a compiled
    texture form, or when it ends in ``c`` and the remainder is a
    non-empty all-alphabetic string (the inverse of the append-``c``
    rule, e.g. ``scriptc``). A bare ``.c`` extension is NOT treated as
    compiled and becomes ``.cc``.
    """
    stem, dot, ext = path.rpartition(".")
    if not dot:
        return path + "c"
    if ext in _COMPILED_TEXTURE_EXTS:
        return path
    if ext in _TEXTURE_SOURCE_EXTS:
        return stem + ".texturesetc"
    if ext in _IMAGE_SOURCE_EXTS:
        return stem + ".texturec"
    if ext.endswith("c") and len(ext) > 1 and ext[:-1].isalpha():
        return path
    return path + "c"


def _encode_varint(value: int) -> bytes:
    """Encode an integer as a minimal-length Protobuf varint.

    Base-128 groups, little-endian group order, high bit set on all but
    the last byte. Values below 128 are a single byte.
    """
    if value < 0:
        raise ValueError(f"varint value must be non-negative, got {value}")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def encode_reload(resources: list[str]) -> bytes:
    """Encode a ``dmResourceDDF.Reload`` body: repeated string field 1.

    Each path is ``0x0a`` + varint byte length + UTF-8 bytes.
    """
    out = bytearray()
    for resource in resources:
        raw = resource.encode("utf-8")
        out += b"\x0a" + _encode_varint(len(raw)) + raw
    return bytes(out)


def encode_system_int32(field_number: int, value: int) -> bytes:
    """Encode an ``int32`` field: tag ``(field_number << 3) | 0`` + varint.

    Negative values are deliberately unsupported: proto2 ``int32``
    negatives are 10-byte two's-complement varints, which the engine
    service routes never need (frequencies, swap intervals and exit
    codes are non-negative).
    """
    if value < 0:
        raise ValueError(
            f"int32 value {value} is negative: proto2 int32 negatives are "
            "10-byte two's-complement varints, which are deliberately "
            "unsupported here"
        )
    return bytes([(field_number << 3) | 0]) + _encode_varint(value)


def encode_system_string(field_number: int, value: str) -> bytes:
    """Encode a string field: tag ``(field_number << 3) | 2`` + length + UTF-8."""
    raw = value.encode("utf-8")
    return bytes([(field_number << 3) | 2]) + _encode_varint(len(raw)) + raw


def _chunk_paths(
    resources: list[str], max_bytes: int = ENGINE_POST_MAX_BYTES
) -> list[tuple[list[str], bytes]]:
    """Batch compiled resource paths into reload POST bodies.

    Greedy: paths accumulate into the current chunk while the per-path
    overhead (1 tag byte + varint length bytes) plus the path fits under
    ``max_bytes``. A single path whose own encoding exceeds the cap is
    still emitted alone; if it cannot fit even alone, ``ValueError`` is
    raised (such a path can never be reloaded via the Engine Service).

    Returns ``(path_group, encoded_body)`` pairs so callers can count
    how many paths each POST carries.
    """
    groups: list[list[str]] = []
    current: list[str] = []
    size = 0
    for resource in resources:
        raw = resource.encode("utf-8")
        overhead = 1 + len(_encode_varint(len(raw)))
        if len(raw) + overhead > max_bytes:
            raise ValueError(
                f"resource path {resource!r} encodes to {len(raw) + overhead} "
                f"bytes, exceeding the {max_bytes}-byte engine POST limit; "
                "it cannot be reloaded"
            )
        if current and size + overhead + len(raw) > max_bytes:
            groups.append(current)
            current = []
            size = 0
        current.append(resource)
        size += overhead + len(raw)
    if current:
        groups.append(current)
    return [(group, encode_reload(group)) for group in groups]


def chunk_reload_posts(
    resources: list[str], max_bytes: int = ENGINE_POST_MAX_BYTES
) -> list[bytes]:
    """Batch compiled resource paths into reload POST bodies.

    Thin wrapper over :func:`_chunk_paths` returning just the encoded
    bodies, without the path groups.
    """
    return [body for _, body in _chunk_paths(resources, max_bytes)]


def discover_engine_port(process_output: str) -> int | None:
    """Extract the Engine Service port from engine stdout.

    Matches ``INFO:ENGINE: Engine service started on port <n>`` over
    multi-line output. Returns ``None`` when the line is absent; callers
    running a standalone ``dmengine`` fall back to ``DEFAULT_ENGINE_PORT``.
    """
    match = ENGINE_PORT_PATTERN.search(process_output)
    return int(match.group(1)) if match else None


class EngineClient:
    """HTTP client for a running Defold Engine Service.

    ``base_url_or_port`` is either a full base URL (``http://127.0.0.1:8123``)
    or a bare port number (bound to ``127.0.0.1``). Pass an ``httpx.Client``
    (e.g. one backed by ``MockTransport``) for hermetic use.
    """

    def __init__(
        self,
        base_url_or_port: str | int,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        if isinstance(base_url_or_port, int):
            self._base_url = f"http://127.0.0.1:{base_url_or_port}"
        else:
            self._base_url = base_url_or_port.rstrip("/")
        self._client = client or httpx.Client()

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Issue an HTTP request against the Engine Service base URL.

        This is the public transport surface: :class:`RecordingClient`
        and other callers should use it rather than reaching into the
        private ``httpx.Client``.
        """
        return self._client.request(method, self._base_url + path, **kwargs)

    def ping(self) -> bool:
        """Return ``True`` when ``GET /ping`` answers ``PONG``."""
        response = self.request("GET", "/ping")
        return response.status_code == 200 and response.text.strip() == "PONG"

    def info(self) -> dict:
        """Return the ``GET /info`` JSON (version, platform, sha1, log_port)."""
        response = self.request("GET", "/info")
        response.raise_for_status()
        return response.json()

    def state(self) -> dict:
        """Return the ``GET /state`` JSON."""
        response = self.request("GET", "/state")
        response.raise_for_status()
        return response.json()

    def post_binary(self, socket: str, message: str, body: bytes) -> httpx.Response:
        """POST a binary Protobuf body to ``/post/<socket>/<message>``.

        Raises ``ValueError`` when the body exceeds ``ENGINE_POST_MAX_BYTES``.
        """
        if len(body) > ENGINE_POST_MAX_BYTES:
            raise ValueError(
                f"engine POST body of {len(body)} bytes exceeds the "
                f"{ENGINE_POST_MAX_BYTES}-byte limit"
            )
        return self.request(
            "POST",
            f"/post/{socket}/{message}",
            content=body,
            headers={"Content-Type": "application/octet-stream"},
        )

    def resource_reload(self, paths: list[str]) -> list[dict]:
        """Reload compiled resources, chunking POSTs to the 1024-byte cap.

        Each path is mapped through :func:`compiled_resource_path`; each
        chunk is POSTed to ``/post/@resource/reload`` and must answer
        2xx — a failing chunk raises ``RuntimeError`` carrying
        ``posted_chunks``, the number of earlier chunks that already
        succeeded. Returns one ``{"posted": n, "bytes": b}`` record per
        chunk.
        """
        compiled = [compiled_resource_path(p) for p in paths]
        results: list[dict] = []
        for index, (group, chunk_body) in enumerate(_chunk_paths(compiled)):
            response = self.post_binary("@resource", "reload", chunk_body)
            if response.status_code >= 300:
                error: RuntimeError = RuntimeError(
                    f"resource reload chunk {index} failed with HTTP "
                    f"{response.status_code}; {index} earlier chunk(s) "
                    f"already succeeded"
                )
                error.posted_chunks = index
                raise error
            results.append({"posted": len(group), "bytes": len(chunk_body)})
        return results

    def toggle_profile(self) -> httpx.Response:
        """POST ``/post/@system/toggle_profile`` with an empty body."""
        return self.post_binary("@system", "toggle_profile", b"")

    def toggle_physics_debug(self) -> httpx.Response:
        """POST ``/post/@system/toggle_physics_debug`` with an empty body."""
        return self.post_binary("@system", "toggle_physics_debug", b"")

    def set_update_frequency(self, frequency: int) -> httpx.Response:
        """POST ``/post/@system/set_update_frequency`` (int32 field 1)."""
        return self.post_binary(
            "@system", "set_update_frequency", encode_system_int32(1, frequency)
        )

    def set_vsync(self, swap_interval: int) -> httpx.Response:
        """POST ``/post/@system/set_vsync`` (int32 field 1)."""
        return self.post_binary(
            "@system", "set_vsync", encode_system_int32(1, swap_interval)
        )

    def exit(self, code: int = 0) -> httpx.Response:
        """POST ``/post/@system/exit`` (int32 field 1)."""
        return self.post_binary("@system", "exit", encode_system_int32(1, code))

    def reboot(self, *args: str) -> httpx.Response:
        """POST ``/post/@system/reboot`` with up to six string args.

        Args map to fields 1..6 (tags ``0x0a, 0x12, 0x1a, 0x22, 0x2a,
        0x32``); only provided args are emitted. Raises ``ValueError``
        beyond six args.
        """
        if len(args) > 6:
            raise ValueError(f"reboot accepts at most six args, got {len(args)}")
        body = b"".join(encode_system_string(i + 1, arg) for i, arg in enumerate(args))
        return self.post_binary("@system", "reboot", body)


class TcpLogCollector:
    """Collect the engine's TCP log stream into a bounded ring buffer.

    The engine speaks first: the first line is a handshake (``"0 OK\\n"``
    — verified to start with ``"0"``, then discarded). Subsequent
    ``\\n``-terminated lines are consumed by a daemon reader thread into
    a ``collections.deque(maxlen=capacity)`` guarded by a lock.

    Offset semantics: the collector tracks ``total_seen`` (every line
    ever received), not just the ring position, so ring eviction never
    corrupts offsets. ``slice(offset, limit, filter)`` clamps the offset
    to the oldest retained line (``start = max(offset, total_seen -
    ring_len)``) and returns the lines at absolute indices
    ``[start, start + limit)`` that are still retained (older lines are
    silently unavailable), plus ``next_offset = start + len(window)``
    where the window is the unfiltered slice — the filter
    (case-insensitive substring) narrows the returned lines but does not
    advance the offset, so filtered reads never skip lines on subsequent
    reads. Reads never consume. Lines are stored without their trailing
    newline.
    """

    def __init__(self, host: str, port: int, *, capacity: int = 5000) -> None:
        self._host = host
        self._port = port
        self._capacity = capacity
        self._ring: collections.deque[str] = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._total = 0
        self._socket: socket.socket | None = None
        self._file = None
        self._thread: threading.Thread | None = None

    def connect(self, timeout: float = 5.0) -> None:
        """Open the socket, verify and discard the handshake, start the reader.

        ``timeout`` bounds both the connect and the handshake read. Any
        failure (bad handshake, timeout, closed connection) closes the
        socket and file and resets the collector's state before the
        exception propagates, so a failed ``connect()`` leaves no
        descriptors behind and ``stop()`` stays a safe no-op.
        """
        sock = socket.create_connection((self._host, self._port), timeout=timeout)
        self._socket = sock
        self._file = sock.makefile("r", encoding="utf-8", errors="replace")
        try:
            first_line = self._file.readline()
        except (TimeoutError, OSError):
            self._teardown()
            raise
        if not first_line.startswith("0"):
            self._teardown()
            raise ConnectionError(
                f"bad engine log handshake: {first_line.strip()!r} "
                "(expected a line starting with '0')"
            )
        # The log stream may be idle for long stretches: no read timeout.
        sock.settimeout(None)
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _teardown(self) -> None:
        """Close the socket and file, resetting state (idempotent)."""
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def _read_loop(self) -> None:
        assert self._file is not None
        try:
            for line in self._file:
                if line.endswith("\n"):
                    line = line[:-1]
                with self._lock:
                    self._ring.append(line)
                    self._total += 1
        except (OSError, ValueError):
            pass  # socket closed by stop() or by the engine

    def slice(
        self, offset: int = 0, limit: int = 100, filter: str | None = None
    ) -> dict:
        """Return ``{"lines": [...], "next_offset": int}`` (see class docstring)."""
        with self._lock:
            total = self._total
            ring = list(self._ring)
        oldest = total - len(ring)
        start = max(offset, oldest)
        window = ring[start - oldest : start - oldest + limit]
        next_offset = start + len(window)
        if filter is not None:
            needle = filter.lower()
            window = [line for line in window if needle in line.lower()]
        return {"lines": window, "next_offset": next_offset}

    def stop(self) -> None:
        """Close the socket and join the reader thread."""
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()
            self._socket = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


class UnsupportedCapability(Exception):
    """Raised when the engine does not support native MP4 recording."""


class RecordingClient:
    """Drive the native MP4 recording protocol (Defold 1.14.1+).

    1. ``GET /recording/capabilities`` — must report ``supported: true``;
       capture ``instance_id``.
    2. ``POST /recording/start`` with ``X-Recording-Instance`` header and
       ``{"fps": ..., "audio": ...}`` -> 202 with ``operation_id``.
    3. ``POST /recording/stop`` (same header, ``{"operation_id": ...}``),
       then poll ``GET /recording/status`` until the file is complete.
    4. ``GET /recording/files/{id}`` — verify byte length AND SHA-256
       against the receipt, save atomically, then ``DELETE`` the file to
       free engine temp storage.
    """

    def __init__(self, engine_client: EngineClient) -> None:
        self._engine = engine_client
        self._operation_id: str | None = None

    def capabilities(self) -> dict:
        """Return the ``GET /recording/capabilities`` JSON."""
        response = self._engine.request("GET", "/recording/capabilities")
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _require_instance_id(caps: dict) -> str:
        """Return the capabilities ``instance_id`` or raise naming the key."""
        instance_id = caps.get("instance_id")
        if instance_id is None:
            raise ValueError(
                f"recording capabilities response is missing 'instance_id': {caps!r}"
            )
        return instance_id

    def start(self, fps: int = 30, audio: bool = True) -> str:
        """Start a recording; returns the ``operation_id``.

        Raises :class:`UnsupportedCapability` when the engine reports
        ``supported != true``.
        """
        caps = self.capabilities()
        if caps.get("supported") is not True:
            raise UnsupportedCapability(
                f"engine does not support native MP4 recording: {caps!r}"
            )
        instance_id = self._require_instance_id(caps)
        response = self._engine.request(
            "POST",
            "/recording/start",
            headers={"X-Recording-Instance": instance_id},
            json={"fps": fps, "audio": audio},
        )
        if response.status_code != 202:
            raise RuntimeError(
                f"recording start returned HTTP {response.status_code}, expected 202"
            )
        start_body = response.json()
        operation_id = start_body.get("operation_id")
        if operation_id is None:
            raise ValueError(
                f"recording start response is missing 'operation_id': {start_body!r}"
            )
        self._operation_id = operation_id
        return self._operation_id

    def stop_and_fetch(
        self,
        out_path: str | Path,
        *,
        poll_interval: float = 0.05,
        timeout: float = 10.0,
    ) -> dict:
        """Stop the recording, wait for finalisation, verify and save the MP4.

        Returns ``{"path", "bytes", "sha256", "operation_id"}``. Raises
        ``httpx.HTTPStatusError`` when ``POST /recording/stop`` fails
        (before any status polling), and ``ValueError`` when the
        downloaded bytes disagree with the receipt (length or SHA-256) —
        the file is not saved in that case; the ``ValueError`` carries
        the engine-side ``file_id`` (also named in the message) so the
        orphaned file can be cleaned up later.
        """
        if self._operation_id is None:
            raise RuntimeError("no recording in progress; call start() first")
        caps = self.capabilities()
        instance_id = self._require_instance_id(caps)
        stop_response = self._engine.request(
            "POST",
            "/recording/stop",
            headers={"X-Recording-Instance": instance_id},
            json={"operation_id": self._operation_id},
        )
        # A failed stop must abort before polling: a stale "complete"
        # receipt from an earlier recording must not be downloaded.
        stop_response.raise_for_status()

        deadline = time.monotonic() + timeout
        file_receipt: dict | None = None
        while True:
            response = self._engine.request("GET", "/recording/status")
            response.raise_for_status()
            status = response.json()
            if (
                status.get("pending") is False
                and status.get("finalized") is True
                and (status.get("file") or {}).get("state") == "complete"
            ):
                file_receipt = status["file"]
                break
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"recording did not finalise within {timeout:.1f}s: {status!r}"
                )
            time.sleep(poll_interval)

        assert file_receipt is not None
        file_id = file_receipt.get("id")
        if file_id is None:
            raise ValueError(
                f"recording status file receipt is missing 'id': {file_receipt!r}"
            )
        download = self._engine.request("GET", f"/recording/files/{file_id}")
        download.raise_for_status()
        data = download.content

        expected_length = file_receipt.get("size")
        if expected_length is not None and len(data) != expected_length:
            error: ValueError = ValueError(
                f"recording length mismatch: got {len(data)} bytes, "
                f"receipt says {expected_length} (file_id={file_id})"
            )
            error.file_id = file_id
            raise error
        expected_sha = file_receipt.get("sha256")
        actual_sha = hashlib.sha256(data).hexdigest()
        if expected_sha is not None and actual_sha != expected_sha:
            error = ValueError(
                f"recording sha256 mismatch: got {actual_sha}, receipt says "
                f"{expected_sha} (file_id={file_id})"
            )
            error.file_id = file_id
            raise error

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=out.parent, prefix=out.name + ".", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp_name, out)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

        self._engine.request("DELETE", f"/recording/files/{file_id}")
        return {
            "path": str(out),
            "bytes": len(data),
            "sha256": actual_sha,
            "operation_id": self._operation_id,
        }
