"""Paths, constants and environment resolution for the defold-mcp server.

This module centralises the URLs, limits and host-platform details the rest
of the server relies on, so that behaviour can be verified in isolation and
overridden via environment variables where appropriate.
"""

import os
import platform
from pathlib import Path

# Base URL for Defold's distribution assets.
D_DEFOLD_BASE = "https://d.defold.com"

# Base URL for Defold GitHub release artifacts (e.g. bob.jar).
GITHUB_RELEASES_BASE = "https://github.com/defold/defold/releases/download"

# Asset store dependency index used for third-party dependency resolution.
ASSET_STORE_JSON_URL = "https://insality.github.io/asset-store/dependencies_store.json"

# Pinned default Defold engine version used when no spec is supplied.
DEFAULT_DEFOLD_VERSION = "1.14.1"

# Defold 1.14.x ships a bob.jar requiring OpenJDK 25 (class file version 69.0).
MIN_BOB_JAVA_MAJOR = 25

# Maximum body size, in bytes, for Engine Service binary Protobuf POSTs.
ENGINE_POST_MAX_BYTES = 1024

# Default port the Defold Engine Service listens on.
DEFAULT_ENGINE_PORT = 8001

# The game.project keys whose values Defold compiles into c-suffixed
# artifacts (e.g. main_collection.c). A tuple is used deliberately: the
# collection must be immutable (it is a fixed engine contract) and ordered
# (downstream tooling may rely on the compile order).
COMPILED_SETTING_KEYS = (
    "bootstrap.main_collection",
    "bootstrap.render",
    "bootstrap.debug_init_script",
    "input.game_binding",
    "input.gamepads",
    "display.display_profiles",
)


def get_cache_dir() -> Path:
    """Return the artifact cache directory.

    Pure path computation with no side effects: honours the
    ``DEFOLD_MCP_CACHE_DIR`` environment variable when set and non-empty,
    otherwise falls back to ``~/.cache/defold-mcp``.
    """
    override = os.environ.get("DEFOLD_MCP_CACHE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "defold-mcp"


def host_platform_info() -> dict:
    """Derive the Defold host target for the running machine.

    Returns a dict with ``os``, ``machine``, ``platform``, ``exe_suffix``
    and ``headless_exe`` keys. Raises ``ValueError`` for unsupported
    platform/machine combinations.
    """
    system = platform.system()
    machine = platform.machine()

    if system == "Linux" and machine in ("x86_64", "amd64"):
        platform_name = "x86_64-linux"
    elif system == "Linux" and machine in ("aarch64", "arm64"):
        platform_name = "arm64-linux"
    elif system == "Darwin" and machine == "arm64":
        platform_name = "arm64-macos"
    elif system == "Darwin" and machine == "x86_64":
        platform_name = "x86_64-macos"
    elif system == "Windows" and machine == "AMD64":
        platform_name = "x86_64-win32"
    else:
        supported = (
            "Linux x86_64/amd64",
            "Linux aarch64/arm64",
            "Darwin arm64",
            "Darwin x86_64",
            "Windows AMD64",
        )
        raise ValueError(
            f"Unsupported platform/machine combination: {system} {machine}. "
            "Supported combinations: " + ", ".join(supported) + "."
        )

    exe_suffix = ".exe" if system == "Windows" else ""
    return {
        "os": system,
        "machine": machine,
        "platform": platform_name,
        "exe_suffix": exe_suffix,
        "headless_exe": f"dmengine_headless{exe_suffix}",
    }
