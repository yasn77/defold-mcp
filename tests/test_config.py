"""Unit tests for defold_mcp.config."""

import platform

import pytest

import defold_mcp.config as config


def test_get_cache_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("DEFOLD_MCP_CACHE_DIR", str(tmp_path / "custom"))
    assert config.get_cache_dir() == tmp_path / "custom"


def test_get_cache_dir_default(tmp_path, monkeypatch):
    monkeypatch.delenv("DEFOLD_MCP_CACHE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.get_cache_dir() == tmp_path / ".cache" / "defold-mcp"


def test_compiled_setting_keys():
    assert config.COMPILED_SETTING_KEYS == (
        "bootstrap.main_collection",
        "bootstrap.render",
        "bootstrap.debug_init_script",
        "input.game_binding",
        "input.gamepads",
        "display.display_profiles",
    )


def test_constants():
    assert config.DEFAULT_DEFOLD_VERSION == "1.14.1"
    assert config.MIN_BOB_JAVA_MAJOR == 25
    assert config.ENGINE_POST_MAX_BYTES == 1024
    assert config.DEFAULT_ENGINE_PORT == 8001


@pytest.mark.skipif(
    not (platform.system() == "Linux" and platform.machine() in ("x86_64", "amd64")),
    reason="asserts the x86_64-linux host target; only valid on such hosts",
)
def test_host_platform_info_returns_supported_shape():
    info = config.host_platform_info()
    assert info["platform"] == "x86_64-linux"
    assert info["exe_suffix"] == ""
    assert info["headless_exe"] == "dmengine_headless"


def test_host_platform_info_unknown_raises(monkeypatch):
    monkeypatch.setattr(config.platform, "system", lambda: "FreeBSD")
    monkeypatch.setattr(config.platform, "machine", lambda: "x86_64")
    # The error message must name the offending pair and list the
    # supported platform/machine pairs.
    with pytest.raises(ValueError) as excinfo:
        config.host_platform_info()
    message = str(excinfo.value)
    assert "FreeBSD x86_64" in message
    assert "Linux x86_64/amd64" in message
    assert "Windows AMD64" in message
