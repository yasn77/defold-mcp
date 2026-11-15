"""Project scaffolding templates for new Defold projects.

Four verified profiles are offered — ``2d_pixel``, ``2d_hd``, ``3d_pbr``
and ``minimal`` — each rendering a small but complete project that opens
in the Defold Editor and compiles headlessly:

* ``game.project`` with profile-appropriate display, physics, HTML5 and
  script defaults (hand-authored, deliberately *not* written through
  :class:`~defold_mcp.project.GameProject`, so the key order stays fixed
  and no comment lines are emitted — bob's parser rejects them).
* ``main/main.collection`` built with
  :func:`~defold_mcp.textproto.serialize_textproto` from the exact tree
  shape the parser + ``decode_embedded_data`` produce, so it round-trips
  byte-exactly.
* ``main/main.script`` (strict Lua 5.1) and ``license/defold_notice.lua``
  carrying the Defold License attribution required when redistributing a
  build (Defold License clause 4(b)).
* ``.gitignore`` and ``.defignore``, already seeded with the entries
  :func:`~defold_mcp.deps.sync_project_deps` expects.

The known third-party libraries in :data:`KNOWN_LIBRARY_URLS` are pinned
to their moving ``master`` archives here; the live phase pins released
tags instead.
"""

from __future__ import annotations

import os
from pathlib import Path

from defold_mcp.textproto import serialize_textproto

__all__ = ["KNOWN_LIBRARY_URLS", "PROFILES", "render_project"]

# Short dependency names the template understands. These point at each
# library's moving default branch; the live phase replaces them with
# released tag archives.
KNOWN_LIBRARY_URLS: dict[str, str] = {
    "deftest": (
        "https://github.com/defold/extension-deftest/"
        "archive/refs/heads/master.zip"
    ),
    "monarch": (
        "https://github.com/britzl/monarch/archive/refs/heads/master.zip"
    ),
    "druid": (
        "https://github.com/Insality/druid/archive/refs/heads/master.zip"
    ),
    "automation_bridge": (
        "https://github.com/defold/extension-automation-bridge/"
        "archive/refs/heads/master.zip"
    ),
}

PROFILES: tuple[str, ...] = ("2d_pixel", "2d_hd", "3d_pbr", "minimal")

_HD_PROFILES = frozenset({"2d_hd", "3d_pbr"})
_3D_PROFILES = frozenset({"3d_pbr"})

_LICENSE_NOTICE = '''-- Defold License attribution (clause 4(b)).
--
-- A build produced from this project embeds the Defold Engine, so this
-- notice must remain part of the distributed game.
local M = {}

M.NOTICE = [[
This software uses the Defold Engine.
Copyright 2009-2024 The Defold Foundation.
Defold is licensed under the Defold License:
https://defold.com/license
]]

return M
'''


def render_project(
    dest_root: "Path | str",
    *,
    title: str,
    profile: str = "2d_pixel",
    dependencies: "list[str] | None" = None,
    with_deftest: bool = False,
) -> dict:
    """Scaffold a new Defold project at ``dest_root``.

    ``title`` becomes ``game.project``'s project title. ``profile`` selects
    one of :data:`PROFILES`; an unknown profile raises ``ValueError``.
    ``dependencies`` accepts full archive URLs or the short names in
    :data:`KNOWN_LIBRARY_URLS`. ``with_deftest`` additionally drops a
    minimal DefTest runner into ``test/``.

    Raises:
        ValueError: on an unknown profile or an unknown dependency name.
        FileExistsError: when ``dest_root`` already holds files.

    Returns a dict with the absolute ``root`` and the created relative
    ``files`` (POSIX separators, sorted).
    """
    if profile not in PROFILES:
        raise ValueError(
            f"unknown profile {profile!r}; expected one of {PROFILES}"
        )
    if not isinstance(title, str) or "\n" in title or "\r" in title:
        raise ValueError("title must be a single-line string")
    root = Path(dest_root)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(
            f"{root} is not empty; refusing to scaffold over existing files"
        )
    root.mkdir(parents=True, exist_ok=True)

    urls = _resolve_dependencies(dependencies or [])

    written: list[str] = []
    _write(root, "game.project", _game_project(title, profile, urls), written)
    _write(
        root,
        "main/main.collection",
        serialize_textproto(_collection_tree(profile)),
        written,
    )
    _write(root, "main/main.script", _main_script(title), written)
    _write(root, "license/defold_notice.lua", _LICENSE_NOTICE, written)
    _write(root, ".gitignore", _gitignore(), written)
    _write(root, ".defignore", _defignore(), written)

    if with_deftest:
        _write(root, "test/test_runner.script", _deftest_script(), written)
        _write(
            root,
            "test/test.collection",
            serialize_textproto(_deftest_collection_tree()),
            written,
        )

    return {"root": str(root), "files": sorted(written)}


# ---------------------------------------------------------------------------
# Dependency resolution
# ---------------------------------------------------------------------------


def _resolve_dependencies(dependencies: list[str]) -> list[str]:
    urls: list[str] = []
    for dependency in dependencies:
        if "://" in dependency:
            urls.append(dependency)
            continue
        try:
            urls.append(KNOWN_LIBRARY_URLS[dependency])
        except KeyError:
            raise ValueError(
                f"unknown dependency {dependency!r}; expected a URL or one "
                f"of {sorted(KNOWN_LIBRARY_URLS)}"
            ) from None
    return urls


# ---------------------------------------------------------------------------
# game.project
# ---------------------------------------------------------------------------


def _game_project(title: str, profile: str, urls: list[str]) -> str:
    """Render ``game.project``.

    Two live-phase findings against Defold 1.14.1 shape the content:

    * bob's ``game.project`` parser rejects any line that is not a section
      header, a ``key = value`` pair or blank — a comment line fails with
      "Could not parse" — so the file carries NO comment lines.
    * Resource-path values are COMPILED paths and must end in ``c``: bob
      strips the final character to locate the source. ``bootstrap``
      therefore names ``...collectionc``, and ``input.game_binding`` points
      at the engine's builtins binding because the default
      ``/input/game.input_binding`` only exists in projects the Editor
      scaffolded. ``bootstrap.debug_init_script`` is left unset: its only
      plausible target, ``/builtins/scripts/debug_init.script``, does not
      ship with the engine, and naming a missing resource fails the build.
    """
    lines = [
        "[project]",
        f'title = "{title}"',
        'version = "0.1.0"',
    ]
    for index, url in enumerate(urls):
        lines.append(f"dependencies#{index} = {url}")

    lines += [
        "",
        "[bootstrap]",
        "main_collection = /main/main.collectionc",
        "render = /builtins/render/default.renderc",
        "",
        "[input]",
        "game_binding = /builtins/input/all.input_bindingc",
        "",
        "[display]",
    ]
    if profile in _HD_PROFILES:
        lines += ["width = 1280", "height = 720"]
    else:
        lines += ["width = 960", "height = 540"]

    lines += ["", "[physics]"]
    if profile in _3D_PROFILES:
        lines += ["type = 3D", "scale = 1.0"]
    else:
        lines += [
            "scale = 0.01",
            "gravity_y = -1000.0",
            "velocity_threshold = 100.0",
            "use_fixed_timestep = 1",
        ]

    if profile not in _3D_PROFILES:
        # Plan §1.5 point 10 pins the fixed timestep under [engine]
        # (``fixed_update_frequency``), not [physics].
        lines += ["", "[engine]", "fixed_update_frequency = 60"]

    lines += [
        "",
        "[html5]",
        "scale_mode = Stretch",
        "heap_size = 64",
        "retry_count = 1000",
        "",
        "[script]",
        "shared_state = 1",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main.collection (built from the canonical parsed-tree shape)
# ---------------------------------------------------------------------------


def _collection_tree(profile: str) -> dict:
    if profile in _3D_PROFILES:
        inner = {
            # The script is mounted for every profile so the licence notice
            # prints at runtime whichever profile was scaffolded.
            "components": [{"id": "script", "component": "/main/main.script"}],
            "embedded_components": [
                {
                    "id": "camera",
                    "type": "camera",
                    "data": "orthographic_projection: 0\n",
                }
            ],
        }
        instance_id = "camera"
    else:
        inner = {
            "components": [{"id": "script", "component": "/main/main.script"}],
            "embedded_components": [
                {
                    "id": "sprite",
                    "type": "sprite",
                    "data": (
                        'default_animation: "idle"\n'
                        'material: "/builtins/materials/sprite.material"\n'
                    ),
                }
            ],
        }
        instance_id = "hero"

    return {
        "name": "main",
        "scale_along_z": 0,
        "embedded_instances": [
            {
                "id": instance_id,
                "data": serialize_textproto(inner),
                "data_ast": inner,
            }
        ],
    }


def _deftest_collection_tree() -> dict:
    return {
        "name": "test",
        "scale_along_z": 0,
        "embedded_instances": [
            {
                "id": "test_runner",
                "data": serialize_textproto(
                    {
                        "components": [
                            {
                                "id": "script",
                                "component": "/test/test_runner.script",
                            }
                        ]
                    }
                ),
                "data_ast": {
                    "components": [
                        {"id": "script", "component": "/test/test_runner.script"}
                    ]
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------


def _lua_string(value: str) -> str:
    """Render ``value`` as the body of a double-quoted Lua string literal."""
    out: list[str] = []
    for char in value:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        else:
            out.append(char)
    return "".join(out)


def _main_script(title: str) -> str:
    return f'''-- Strict Lua 5.1 (HTML5-safe): no goto, no integer division.
-- Per-instance state lives on `self`; module-level locals are shared
-- across every instance of this script.

go.property("speed", 10)

function init(self)
    print("{_lua_string(title)}")
    print(require("license.defold_notice").NOTICE)
end

function update(self, dt)
    -- Delete this callback when the script needs no per-frame work.
end
'''


def _deftest_script() -> str:
    return '''-- Minimal DefTest runner. Add a dependency on
-- `https://github.com/defold/extension-deftest` and start the collection
-- from `defold_test_run`.

local deftest = require("deftest.deftest")

function init(self)
    deftest.log()
end
'''


# ---------------------------------------------------------------------------
# Ignore files
# ---------------------------------------------------------------------------


def _gitignore() -> str:
    return "/.deps\n/build\n/.internal\n"


def _defignore() -> str:
    return "/.deps\n/build\n"


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _write(root: Path, relative: str, text: str, written: list[str]) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.replace(tmp, path)
    written.append(relative)
