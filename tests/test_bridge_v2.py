"""Hermetic tests for the extension-automation-bridge v2 client.

All HTTP goes through ``httpx.MockTransport``. The DLIB quirk (logical
errors arriving as HTTP 500 with a structured JSON body) is exercised
directly. No real engine, no external network.
"""

import hashlib
import json
import time
import uuid

import httpx
import pytest

from defold_mcp.bridge_client import BridgeApiError, BridgeClient

BASE = "http://127.0.0.1:9999"
PREFIX = "/automation-bridge/v2"


def make_client(handler) -> BridgeClient:
    transport = httpx.MockTransport(handler)
    return BridgeClient(BASE, client=httpx.Client(transport=transport))


# -- DLIB 500 unwrapping -----------------------------------------------------


def test_dlib_500_unwrapped_structurally() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"{PREFIX}/scene"
        return httpx.Response(
            500,
            json={
                "ok": False,
                "error": {"status": 409, "code": "stale_scene", "message": "scene moved"},
            },
        )

    client = make_client(handler)
    with pytest.raises(BridgeApiError) as excinfo:
        client.scene()
    exc = excinfo.value
    assert exc.status == 409
    assert exc.code == "stale_scene"
    assert exc.message == "scene moved"
    assert exc.http_status == 500
    rendered = str(exc)
    assert "409" in rendered
    assert "stale_scene" in rendered
    assert "scene moved" in rendered


def test_raw_500_without_json_degrades() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom: no json here")

    client = make_client(handler)
    with pytest.raises(BridgeApiError) as excinfo:
        client.health()
    exc = excinfo.value
    assert exc.status == 500
    assert exc.code is None
    assert "boom: no json here" in exc.message
    assert "boom: no json here" in str(exc)


# -- health / scene / elements -----------------------------------------------


def test_health_scene_elements() -> None:
    element_calls: list[httpx.Request] = []
    element_shapes = [
        {"ok": True, "elements": [{"id": "e1", "name": "btn_a"}]},
        [{"id": "e2", "name": "btn_b"}],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"{PREFIX}/health":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "scene_sequence": 7,
                    "engine_frame": 1234,
                    "screen": {"width": 1280, "height": 720},
                    "input": {"devices": ["mouse"]},
                },
            )
        if path == f"{PREFIX}/scene":
            assert request.url.params["visible"] == "1"
            assert request.url.params["include"] == "bounds,properties"
            return httpx.Response(
                200, json={"ok": True, "scene_sequence": 7, "nodes": [{"name": "root"}]}
            )
        if path == f"{PREFIX}/elements":
            element_calls.append(request)
            return httpx.Response(200, json=element_shapes[len(element_calls) - 1])
        raise AssertionError(f"unexpected {request.method} {path}")

    client = make_client(handler)
    assert client.health()["scene_sequence"] == 7
    assert client.scene()["nodes"] == [{"name": "root"}]

    # Both response shapes are tolerated; the elements list is returned.
    assert client.elements(automation_id="play_btn") == [{"id": "e1", "name": "btn_a"}]
    assert client.elements(type="button") == [{"id": "e2", "name": "btn_b"}]

    first, second = element_calls
    assert first.url.params["visible"] == "1"
    assert first.url.params["limit"] == "50"
    assert first.url.params["automation_id"] == "play_btn"
    assert "type" not in first.url.params
    assert second.url.params["type"] == "button"
    assert "automation_id" not in second.url.params


# -- lease management ---------------------------------------------------------


def test_ensure_lease_configures() -> None:
    puts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path == f"{PREFIX}/input/configure":
            puts.append(request)
            return httpx.Response(200, json={"ok": True, "leased": True})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    client = make_client(handler)
    state = client.ensure_lease()
    assert state == {"ok": True, "leased": True}
    assert len(puts) == 1
    params = puts[0].url.params
    assert params["client_id"] == "defold-mcp"
    assert params["session_id"] == str(uuid.UUID(params["session_id"]))  # a UUID
    assert params["lease"] == "10"
    assert params["device"] == "auto"
    assert params["visualize"] == "1"

    # A second ensure_lease within validity must not re-PUT.
    client.ensure_lease()
    assert len(puts) == 1


def test_lease_renews_on_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    puts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "PUT" and path == f"{PREFIX}/input/configure":
            puts.append(request)
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST" and path == f"{PREFIX}/input/click":
            return httpx.Response(200, json={"input_id": "i1"})
        if path == f"{PREFIX}/input/status":
            return httpx.Response(200, json={"state": "released"})
        raise AssertionError(f"unexpected {request.method} {path}")

    client = make_client(handler)
    client.ensure_lease(lease_seconds=10)
    assert len(puts) == 1

    # Advance the clock past the lease expiry; the next input call must
    # re-configure with the SAME session_id.
    real_monotonic = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic + 11)
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    result = client.click(x=1, y=2)
    assert result["state"] == "released"
    assert len(puts) == 2
    assert puts[1].url.params["session_id"] == puts[0].url.params["session_id"]
    assert puts[1].url.params["lease"] == "10"


# -- click --------------------------------------------------------------------


def test_click_by_automation_id_full_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    state = {"status_polls": 0, "puts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "PUT" and path == f"{PREFIX}/input/configure":
            state["puts"] += 1
            return httpx.Response(200, json={"ok": True})
        if path == f"{PREFIX}/elements":
            assert request.url.params["automation_id"] == "play_btn"
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {
                            "id": "e1",
                            "scene_sequence": 7,
                            "logical_id": "L1",
                            "bounds": {"center": [100, 200]},
                        }
                    ]
                },
            )
        if request.method == "POST" and path == f"{PREFIX}/input/click":
            body = json.loads(request.content)
            assert body["id"] == "e1"
            assert body["expected_scene_sequence"] == 7
            assert body["expected_logical_id"] == "L1"
            return httpx.Response(200, json={"input_id": "i1"})
        if path == f"{PREFIX}/input/status":
            assert request.url.params["input_id"] == "i1"
            state["status_polls"] += 1
            if state["status_polls"] == 1:
                return httpx.Response(200, json={"state": "pending"})
            return httpx.Response(200, json={"state": "released", "input_id": "i1"})
        raise AssertionError(f"unexpected {request.method} {path}")

    client = make_client(handler)
    result = client.click(automation_id="play_btn")
    assert result == {"state": "released", "input_id": "i1"}
    assert state["status_polls"] == 2
    assert state["puts"] == 1  # the lease was acquired once, before the click


def test_click_by_automation_id_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path == f"{PREFIX}/input/configure":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == f"{PREFIX}/elements":
            return httpx.Response(200, json={"elements": []})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    client = make_client(handler)
    with pytest.raises(LookupError, match="play_btn"):
        client.click(automation_id="play_btn")


def test_click_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "PUT" and path == f"{PREFIX}/input/configure":
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST" and path == f"{PREFIX}/input/click":
            return httpx.Response(200, json={"input_id": "i2"})
        if path == f"{PREFIX}/input/status":
            state["polls"] += 1
            return httpx.Response(200, json={"state": "pending"})
        raise AssertionError(f"unexpected {request.method} {path}")

    client = make_client(handler)
    with pytest.raises(TimeoutError) as excinfo:
        client.click(x=5, y=6, poll_timeout=0.05)
    assert excinfo.value.last_status == {"state": "pending"}
    assert state["polls"] >= 1


# -- screenshots ---------------------------------------------------------------


def _screenshot_handler(tmp_path, payload: bytes, receipt_sha: str, poll_count: int):
    shot = tmp_path / "shot.png"
    shot.write_bytes(payload)
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"{PREFIX}/screenshot":
            assert request.url.params["after_frames"] == "1"
            return httpx.Response(200, json={"capture_id": "c1"})
        if path == f"{PREFIX}/screenshot/status":
            assert request.url.params["capture_id"] == "c1"
            state["polls"] += 1
            if state["polls"] < poll_count:
                return httpx.Response(200, json={"state": "working"})
            return httpx.Response(
                200,
                json={"state": "complete", "path": str(shot), "sha256": receipt_sha},
            )
        raise AssertionError(f"unexpected {request.method} {path}")

    return handler, state, shot


def test_take_screenshot_full_flow(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    payload = b"\x89PNG fake bytes 12345"
    sha = hashlib.sha256(payload).hexdigest()
    handler, state, shot = _screenshot_handler(tmp_path, payload, sha, poll_count=3)

    client = make_client(handler)
    result = client.take_screenshot()
    assert result == {
        "capture_id": "c1",
        "path": str(shot),
        "sha256": sha,
        "verified": True,
    }
    assert state["polls"] == 3  # two "working" polls, then "complete"


def test_take_screenshot_sha_mismatch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    payload = b"the real bytes"
    wrong_sha = hashlib.sha256(b"different bytes").hexdigest()
    handler, _state, _shot = _screenshot_handler(tmp_path, payload, wrong_sha, poll_count=1)

    client = make_client(handler)
    with pytest.raises(BridgeApiError) as excinfo:
        client.take_screenshot()
    assert excinfo.value.code == "sha256_mismatch"


def test_take_screenshot_no_sha256_not_verified(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A completion receipt without a sha256 verifies nothing: "verified" must
    # be False (not a blanket True).
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"some bytes")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"{PREFIX}/screenshot":
            return httpx.Response(200, json={"capture_id": "c1"})
        if path == f"{PREFIX}/screenshot/status":
            return httpx.Response(
                200, json={"state": "complete", "path": str(shot)}
            )
        raise AssertionError(f"unexpected {request.method} {path}")

    client = make_client(handler)
    result = client.take_screenshot()
    assert result["verified"] is False
    assert result["sha256"] is None
    assert result["path"] == str(shot)


def test_take_screenshot_missing_capture_id() -> None:
    # A 200 screenshot body without capture_id must raise the client's own
    # error (ValueError), not a raw KeyError.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    with pytest.raises(ValueError, match="capture_id"):
        client.take_screenshot()
