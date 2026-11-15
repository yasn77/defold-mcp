"""Client for the community ``defold/extension-automation-bridge`` v2.

The extension (enabled by adding the dependency and registering it)
exposes live-scene inspection, input injection and post-render
screenshots on the RUNNING ENGINE under the base URL
``http://127.0.0.1:<engine_port>/automation-bridge/v2``.

DLIB HTTP status quirk: Defold's embedded C++ web server only knows
reason phrases for 200/302/404/500, so logical errors arrive as HTTP
500 carrying ``{"ok": false, "error": {"status": 409, "code": ...,
"message": ...}}``. :meth:`BridgeClient._request` therefore always
inspects the JSON body on non-2xx responses and raises
:class:`BridgeApiError` with the LOGICAL status/code — the raw HTTP
status is retained only as ``http_status``.

Input actions run under a FIFO input lease acquired via
``PUT /input/configure`` (query params, not JSON); the lease expires
after ``lease`` seconds and mutating calls renew it automatically
before use.
"""

import hashlib
import time
import uuid
from pathlib import Path

import httpx

# Poll cadence for input/screenshot status endpoints (seconds).
_POLL_INTERVAL = 0.05


class BridgeApiError(Exception):
    """A structured error from the automation bridge.

    ``status`` is the LOGICAL status from the JSON error body (e.g. 409
    for a stale scene guard), not the raw HTTP status — DLIB wraps
    logical errors in HTTP 500. ``code`` is the machine-readable error
    code (e.g. ``"stale_scene"``); ``message`` is the human-readable
    text. ``http_status`` carries the raw HTTP status when one was
    involved (``None`` for locally detected errors such as a
    screenshot hash mismatch).
    """

    def __init__(
        self,
        status: int,
        *,
        code: str | None = None,
        message: str = "",
        http_status: int | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(self.__str__())

    def __str__(self) -> str:
        parts = []
        if self.code is not None:
            parts.append(f"code={self.code}")
        parts.append(f"status={self.status}")
        if self.message:
            parts.append(f"message={self.message!r}")
        return " ".join(parts)


class BridgeClient:
    """HTTP client for the extension-automation-bridge v2 API.

    ``engine_base_url_or_port`` is either a full base URL
    (``http://127.0.0.1:8123``) or a bare port number (bound to
    ``127.0.0.1``); ``/automation-bridge/v2`` is appended in both
    cases. Pass an ``httpx.Client`` (e.g. one backed by
    ``MockTransport``) for hermetic use.
    """

    def __init__(
        self,
        engine_base_url_or_port: str | int,
        *,
        client: httpx.Client | None = None,
        client_id: str = "defold-mcp",
    ) -> None:
        if isinstance(engine_base_url_or_port, int):
            base = f"http://127.0.0.1:{engine_base_url_or_port}"
        else:
            base = engine_base_url_or_port.rstrip("/")
        self._base_url = base + "/automation-bridge/v2"
        self._client = client or httpx.Client()
        self._client_id = client_id
        self._session_id: str | None = None
        self._lease_seconds = 10
        self._lease_expiry = 0.0  # monotonic; 0 means "no lease"
        self._lease_state: dict = {}

    # -- transport -----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Perform a bridge request and return the parsed JSON body.

        On non-2xx responses the body is parsed as JSON: a dict with
        ``ok is False`` and an ``error`` sub-dict raises
        :class:`BridgeApiError` with the logical
        ``error.status``/``error.code``/``error.message``; anything
        else (including malformed JSON) degrades to a
        :class:`BridgeApiError` carrying the raw HTTP status and body
        text. 2xx responses return the parsed JSON; a non-JSON 2xx
        body is tolerated as ``{"ok": True, "raw": <text>}``.
        """
        response = self._client.request(method, self._base_url + path, **kwargs)
        if response.status_code < 400:
            try:
                return response.json()
            except ValueError:
                return {"ok": True, "raw": response.text}
        try:
            body = response.json()
        except ValueError:
            body = None
        if (
            isinstance(body, dict)
            and body.get("ok") is False
            and isinstance(body.get("error"), dict)
        ):
            error = body["error"]
            raise BridgeApiError(
                error.get("status", response.status_code),
                code=error.get("code"),
                message=error.get("message", ""),
                http_status=response.status_code,
            )
        raise BridgeApiError(
            response.status_code,
            message=response.text,
            http_status=response.status_code,
        )

    # -- inspection ----------------------------------------------------------

    def health(self) -> dict:
        """Return ``GET /health`` (capabilities, scene_sequence, engine_frame)."""
        return self._request("GET", "/health")

    def scene(self, *, visible: bool = True, include: str = "bounds,properties") -> dict:
        """Return ``GET /scene`` — the full live scene hierarchy."""
        return self._request(
            "GET",
            "/scene",
            params={"visible": "1" if visible else "0", "include": include},
        )

    def elements(
        self,
        *,
        type: str | None = None,
        name: str | None = None,
        text: str | None = None,
        automation_id: str | None = None,
        visible: bool = True,
        limit: int = 50,
    ) -> list[dict]:
        """Return ``GET /elements`` — a paginated element search.

        Each element carries top-left-window-pixel ``bounds.center``,
        a ``logical_id`` and an ``id``. Both response shapes are
        tolerated: ``{"elements": [...]}`` (the ``elements`` list is
        returned) and a bare JSON list (returned as-is); a dict
        without an ``elements`` key yields an empty list.
        """
        params: dict[str, str] = {
            "visible": "1" if visible else "0",
            "limit": str(limit),
        }
        for key, value in (
            ("type", type),
            ("name", name),
            ("text", text),
            ("automation_id", automation_id),
        ):
            if value is not None:
                params[key] = value
        response = self._request("GET", "/elements", params=params)
        if isinstance(response, list):
            return response
        return response.get("elements", [])

    # -- input lease ----------------------------------------------------------

    def ensure_lease(
        self, *, lease_seconds: int = 10, session_id: str | None = None
    ) -> dict:
        """Acquire (or reuse) the FIFO input lease; return the lease state.

        ``PUT /input/configure`` with query params ``client_id``,
        ``session_id`` (a fresh ``uuid4`` when not supplied; sticky per
        client instance once created), ``lease``, ``device=auto`` and
        ``visualize=1``. The expiry is tracked with
        ``time.monotonic() + lease_seconds``; a call within the
        validity window is a no-op.
        """
        if self._session_id is None:
            self._session_id = session_id or str(uuid.uuid4())
        if self._lease_valid():
            return self._lease_state
        self._lease_seconds = lease_seconds
        return self._configure(lease_seconds)

    def _configure(self, lease_seconds: int) -> dict:
        if self._session_id is None:
            self._session_id = str(uuid.uuid4())
        params = {
            "client_id": self._client_id,
            "session_id": self._session_id,
            "lease": str(lease_seconds),
            "device": "auto",
            "visualize": "1",
        }
        response = self._request("PUT", "/input/configure", params=params)
        self._lease_expiry = time.monotonic() + lease_seconds
        self._lease_state = response if isinstance(response, dict) else {"ok": True}
        return self._lease_state

    def _lease_valid(self) -> bool:
        return time.monotonic() < self._lease_expiry

    def release_lease(self) -> dict:
        """Release the input lease.

        Convention: ``PUT /input/configure`` with ``lease=0``. If the
        extension rejects the call (any :class:`BridgeApiError`), this
        degrades to a no-op returning a warning dict — release never
        raises.
        """
        if self._session_id is None:
            return {"ok": True, "warning": "no lease was ever acquired"}
        params = {
            "client_id": self._client_id,
            "session_id": self._session_id,
            "lease": "0",
            "device": "auto",
            "visualize": "1",
        }
        try:
            response = self._request("PUT", "/input/configure", params=params)
        except BridgeApiError as exc:
            self._lease_expiry = 0.0
            return {"ok": False, "warning": f"lease release rejected: {exc}"}
        self._lease_expiry = 0.0
        return response if isinstance(response, dict) else {"ok": True}

    # -- input actions ---------------------------------------------------------

    def _resolve_target(
        self,
        *,
        element: dict | None,
        automation_id: str | None,
        x: float | None,
        y: float | None,
        expected_scene_sequence: int | None,
        expected_logical_id: str | None,
    ) -> dict:
        """Build the target/guard portion of an input action body.

        ``element`` (a dict with ``id`` plus its ``scene_sequence`` /
        ``logical_id`` guards) is used directly; ``automation_id``
        resolves via :meth:`elements` taking the FIRST match (a
        ``LookupError`` naming the automation_id when none); explicit
        ``x``/``y`` are sent as coordinates. ``None`` fields are
        omitted from the body.
        """
        body: dict = {}
        if element is not None:
            element_id = element.get("id")
            if element_id is None:
                raise ValueError(f"element dict is missing 'id': {element!r}")
            body["id"] = element_id
            if expected_scene_sequence is None:
                expected_scene_sequence = element.get("scene_sequence")
            if expected_logical_id is None:
                expected_logical_id = element.get("logical_id")
        elif automation_id is not None:
            matches = self.elements(automation_id=automation_id)
            if not matches:
                raise LookupError(f"no element with automation_id {automation_id!r}")
            match = matches[0]
            match_id = match.get("id")
            if match_id is None:
                raise ValueError(
                    f"element match for automation_id {automation_id!r} "
                    f"is missing 'id': {match!r}"
                )
            body["id"] = match_id
            if expected_scene_sequence is None:
                expected_scene_sequence = matches[0].get("scene_sequence")
            if expected_logical_id is None:
                expected_logical_id = matches[0].get("logical_id")
        if x is not None:
            body["x"] = x
        if y is not None:
            body["y"] = y
        if expected_scene_sequence is not None:
            body["expected_scene_sequence"] = expected_scene_sequence
        if expected_logical_id is not None:
            body["expected_logical_id"] = expected_logical_id
        return body

    def _perform_input(self, action: str, body: dict, *, poll_timeout: float) -> dict:
        """Ensure the lease, POST the action, poll until released.

        Returns the final ``GET /input/status`` dict (``state ==
        "released"``). Raises ``TimeoutError`` (carrying
        ``last_status``) when the input is not released within
        ``poll_timeout`` seconds.
        """
        if not self._lease_valid():
            self._configure(self._lease_seconds)
        response = self._request("POST", f"/input/{action}", json=body)
        input_id = response.get("input_id")
        if input_id is None:
            raise ValueError(
                f"input {action!r} response is missing 'input_id': {response!r}"
            )
        deadline = time.monotonic() + poll_timeout
        last: dict = {}
        while True:
            last = self._request("GET", "/input/status", params={"input_id": input_id})
            if last.get("state") == "released":
                return last
            if time.monotonic() >= deadline:
                error = TimeoutError(
                    f"input {input_id!r} not released within {poll_timeout:.2f}s: {last!r}"
                )
                error.last_status = last
                raise error
            time.sleep(_POLL_INTERVAL)

    def click(
        self,
        *,
        automation_id: str | None = None,
        element: dict | None = None,
        x: float | None = None,
        y: float | None = None,
        expected_scene_sequence: int | None = None,
        expected_logical_id: str | None = None,
        poll_timeout: float = 5.0,
    ) -> dict:
        """``POST /input/click`` — click an element or explicit coordinates.

        See :meth:`_resolve_target` for target resolution. The body
        carries ``id``/``x``/``y`` plus the ``expected_scene_sequence``
        / ``expected_logical_id`` guards (``None`` fields omitted).
        """
        body = self._resolve_target(
            element=element,
            automation_id=automation_id,
            x=x,
            y=y,
            expected_scene_sequence=expected_scene_sequence,
            expected_logical_id=expected_logical_id,
        )
        return self._perform_input("click", body, poll_timeout=poll_timeout)

    def drag(
        self,
        *,
        from_point: tuple[float, float],
        to_point: tuple[float, float],
        element: dict | None = None,
        automation_id: str | None = None,
        expected_scene_sequence: int | None = None,
        expected_logical_id: str | None = None,
        poll_timeout: float = 5.0,
    ) -> dict:
        """``POST /input/drag`` — drag from one point to another.

        Body: ``{"id": …, "from": [x, y], "to": [x, y], guards}``.
        """
        body = self._resolve_target(
            element=element,
            automation_id=automation_id,
            x=None,
            y=None,
            expected_scene_sequence=expected_scene_sequence,
            expected_logical_id=expected_logical_id,
        )
        body["from"] = list(from_point)
        body["to"] = list(to_point)
        return self._perform_input("drag", body, poll_timeout=poll_timeout)

    def drag_path(
        self,
        *,
        points: list[tuple[float, float]],
        element: dict | None = None,
        automation_id: str | None = None,
        expected_scene_sequence: int | None = None,
        expected_logical_id: str | None = None,
        poll_timeout: float = 5.0,
    ) -> dict:
        """``POST /input/drag_path`` — drag along a polyline.

        Body: ``{"id": …, "points": [[x, y], …], guards}``.
        """
        body = self._resolve_target(
            element=element,
            automation_id=automation_id,
            x=None,
            y=None,
            expected_scene_sequence=expected_scene_sequence,
            expected_logical_id=expected_logical_id,
        )
        body["points"] = [list(point) for point in points]
        return self._perform_input("drag_path", body, poll_timeout=poll_timeout)

    def key(
        self,
        *,
        action: str = "press",
        key: str,
        element: dict | None = None,
        automation_id: str | None = None,
        expected_scene_sequence: int | None = None,
        expected_logical_id: str | None = None,
        poll_timeout: float = 5.0,
    ) -> dict:
        """``POST /input/key`` — press, release or tap a key.

        Body: ``{"action": "press"|"release"|"tap", "key": "<name>",
        "id": …, guards}``.
        """
        if action not in ("press", "release", "tap"):
            raise ValueError(f"key action must be press, release or tap, got {action!r}")
        body = self._resolve_target(
            element=element,
            automation_id=automation_id,
            x=None,
            y=None,
            expected_scene_sequence=expected_scene_sequence,
            expected_logical_id=expected_logical_id,
        )
        body["action"] = action
        body["key"] = key
        return self._perform_input("key", body, poll_timeout=poll_timeout)

    def wheel(
        self,
        *,
        delta_x: float,
        delta_y: float,
        element: dict | None = None,
        automation_id: str | None = None,
        expected_scene_sequence: int | None = None,
        expected_logical_id: str | None = None,
        poll_timeout: float = 5.0,
    ) -> dict:
        """``POST /input/wheel`` — scroll the wheel.

        Body: ``{"delta_x": …, "delta_y": …, "id": …, guards}``.
        """
        body = self._resolve_target(
            element=element,
            automation_id=automation_id,
            x=None,
            y=None,
            expected_scene_sequence=expected_scene_sequence,
            expected_logical_id=expected_logical_id,
        )
        body["delta_x"] = delta_x
        body["delta_y"] = delta_y
        return self._perform_input("wheel", body, poll_timeout=poll_timeout)

    # -- screenshots -----------------------------------------------------------

    def take_screenshot(self, *, after_frames: int = 1, poll_timeout: float = 10.0) -> dict:
        """Capture a post-render screenshot and verify its receipt hash.

        ``GET /screenshot?after_frames=…`` returns a ``capture_id``;
        ``GET /screenshot/status`` is polled until ``state ==
        "complete"`` (``TimeoutError`` carrying ``last_status``
        otherwise). The completion receipt carries an engine-side
        ``path`` (a plain filesystem path) and a ``sha256``; when the
        receipt has a ``sha256`` the file is read from disk and the
        hash verified — a mismatch raises
        :class:`BridgeApiError` with ``code="sha256_mismatch"``.
        Returns ``{"capture_id", "path", "sha256", "verified"}`` where
        ``verified`` is ``True`` only when the receipt carried a
        ``sha256`` and it matched the file on disk.
        """
        response = self._request(
            "GET", "/screenshot", params={"after_frames": str(after_frames)}
        )
        capture_id = response.get("capture_id")
        if capture_id is None:
            raise ValueError(
                f"screenshot response is missing 'capture_id': {response!r}"
            )
        deadline = time.monotonic() + poll_timeout
        last: dict = {}
        while True:
            last = self._request(
                "GET", "/screenshot/status", params={"capture_id": capture_id}
            )
            if last.get("state") == "complete":
                break
            if time.monotonic() >= deadline:
                error = TimeoutError(
                    f"screenshot {capture_id!r} not complete within "
                    f"{poll_timeout:.2f}s: {last!r}"
                )
                error.last_status = last
                raise error
            time.sleep(_POLL_INTERVAL)

        path = last.get("path")
        if path is None:
            raise ValueError(
                f"screenshot completion receipt is missing 'path': {last!r}"
            )
        sha256 = last.get("sha256")
        if sha256 is not None:
            actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            if actual != sha256:
                raise BridgeApiError(
                    500,
                    code="sha256_mismatch",
                    message=(
                        f"screenshot receipt hash mismatch for {path!r}: "
                        f"got {actual}, receipt says {sha256}"
                    ),
                )
        return {
            "capture_id": capture_id,
            "path": path,
            "sha256": sha256,
            "verified": sha256 is not None,
        }
