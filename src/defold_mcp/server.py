"""FastMCP server wiring every Defold tool.

:func:`build_server` returns an ``MCPServer`` (the ``mcp`` 2.x name for
``FastMCP``) named ``defold`` with the twenty tools of the plan's §2.3
catalogue registered in order. Each tool is a thin, synchronous wrapper
around one of the subsystem modules; it resolves the project root, calls
into the subsystem, and returns a JSON string built by :func:`_respond`.

Conventions honoured by every tool:

* ``project_root`` resolution runs through
  :func:`defold_mcp.project.resolve_project_root` — explicit argument,
  then ``DEFOLD_PROJECT_ROOT``, then an upward ``game.project`` search.
* Responses are capped at 25,000 characters and flagged with
  ``"truncated": true`` plus a continuation hint when they exceed it.
* Project-scoped responses carry ``"backend": "editor" | "headless"``,
  probed per call through :func:`defold_mcp.editor_client.probe_editor`
  (never cached, so a newly opened Editor is picked up immediately).
* Nothing is ever written to stdout: stdout is reserved for JSON-RPC
  frames under the stdio transport, and all diagnostics go to stderr via
  the standard :mod:`logging` module.

Long-lived state lives in module-level registries keyed by the resolved
project root: spawned engine processes with their stdout rings
(:data:`_RUNNING`), and engine TCP log collectors
(:data:`_TCP_COLLECTORS`). ``defold_run`` replaces any engine already
registered for the root (never orphaning it) and attaches a TCP collector
when the engine advertises a ``log_port``; ``defold_stop`` pairs that
cleanup by stopping the collector and terminating the process.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from defold_mcp import (
    bob_runner,
    bridge_client,
    deps,
    docs_index,
    editor_client,
    engine_client,
    templates,
    test_runner,
    toolchain,
)
from defold_mcp.config import DEFAULT_DEFOLD_VERSION, get_cache_dir
from defold_mcp.project import GameProject, resolve_project_root
from defold_mcp.textproto import (
    decode_embedded_data,
    parse_textproto,
    serialize_textproto,
)
from defold_mcp.textproto.mutations import apply_operations
from defold_mcp.textproto.schemas import ALWAYS_LIST_FIELDS

logger = logging.getLogger("defold_mcp.server")

#: Hard ceiling on a single tool response, in characters.
MAX_RESPONSE_CHARS = 25_000

#: Trailing output kept when a build fails before a tool can summarise it.
_DIAGNOSTIC_TAIL_CHARS = 2_000

_TRUNCATION_HINT = (
    "Response truncated at 25,000 characters; narrow the query or use "
    "offset/limit to continue."
)

#: Source extensions rendered as a Lua outline instead of a proto tree.
_LUA_SOURCE_SUFFIXES = frozenset(
    {".script", ".lua", ".gui_script", ".render_script"}
)

#: Directories never walked when inventorying a project.
_SKIP_DIRS = frozenset({"build", ".internal", ".git"})

#: Spawned engine processes keyed by resolved project root.
_RUNNING: dict[str, dict] = {}

#: Engine TCP log collectors keyed by resolved project root.
_TCP_COLLECTORS: dict[str, engine_client.TcpLogCollector] = {}

_RING_CAPACITY = 5_000

_DOC_EXTENSIONS = frozenset(
    {
        ".collection",
        ".go",
        ".gui",
        ".atlas",
        ".tilesource",
        ".tilemap",
        ".collisionobject",
        ".sprite",
        ".factory",
        ".material",
        ".particlefx",
        ".font",
        ".label",
        ".model",
        ".script",
        ".gui_script",
        ".render_script",
        ".lua",
        ".vp",
        ".fp",
        ".glsl",
        ".png",
        ".jpg",
        ".jpeg",
        ".tga",
        ".wav",
        ".ogg",
        ".opus",
    }
)

_PROPERTY_RE = re.compile(r"go\.property\(\s*\"([^\"]+)\"")
_FUNCTION_RE = re.compile(
    r"^[ \t]*(?:local[ \t]+)?function[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*\(",
    re.MULTILINE,
)
_REQUIRE_RE = re.compile(r"require\(\s*\"([^\"]+)\"\s*\)")
_MSG_POST_RE = re.compile(r"msg\.post\(\s*\"([^\"]+)\"")
_HASH_RE = re.compile(r"hash\(\s*\"([^\"]+)\"\s*\)")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _resolve_root(project_root: str | None) -> Path:
    """Resolve the project root through the standard chain."""
    return resolve_project_root(project_root)


def _try_resolve_root(project_root: str | None) -> Path | None:
    """Resolve the project root, returning ``None`` when it cannot be found."""
    try:
        return resolve_project_root(project_root)
    except FileNotFoundError:
        return None


def _backend(root: Path) -> str:
    """Probe the running Editor for ``root``; never cached."""
    return (
        "editor"
        if editor_client.probe_editor(root) is not None
        else "headless"
    )


def _resource_path(root: Path, resource_path: str) -> Path:
    """Map a ``/main/hero.go`` resource path onto the filesystem.

    The result is resolved and asserted to stay inside the project root, so
    a ``..``-bearing path can neither read nor overwrite anything outside
    it. ``lstrip("/")`` alone does not neutralise traversal.
    """
    resolved_root = root.resolve()
    candidate = (resolved_root / resource_path.lstrip("/")).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ValueError(
            f"resource path {resource_path!r} escapes the project root"
        )
    return candidate


def _truncate(payload: dict) -> str:
    """Serialise ``payload``, shrinking it until it fits the response cap.

    Adding the ``truncated``/``hint`` markers can never shrink an
    over-budget dump, so the over-budget path returns a bounded envelope
    whose ``preview`` is trimmed until the *envelope itself* fits. Trimming
    is iterative because re-serialising a slice of JSON re-escapes it, which
    can expand the text past the cap again.
    """
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= MAX_RESPONSE_CHARS:
        return text

    envelope: dict = {"ok": payload.get("ok", True)}
    if payload.get("backend") is not None:
        envelope["backend"] = payload["backend"]
    envelope["truncated"] = True
    envelope["hint"] = _TRUNCATION_HINT
    envelope["preview"] = ""

    budget = MAX_RESPONSE_CHARS
    while budget > 0:
        envelope["preview"] = text[:budget]
        rendered = json.dumps(envelope, ensure_ascii=False)
        if len(rendered) <= MAX_RESPONSE_CHARS:
            return rendered
        # Shrink geometrically (with a guaranteed step) so an
        # escaping-heavy preview converges instead of collapsing to empty.
        scaled = int(budget * MAX_RESPONSE_CHARS / len(rendered)) - 64
        budget = min(budget - 64, scaled)

    envelope["preview"] = ""
    return json.dumps(envelope, ensure_ascii=False)


def _respond(payload: dict, *, backend: str | None = None) -> str:
    """Render a tool response: JSON, backend stamp and the 25k cap."""
    data = dict(payload)
    if backend is not None:
        data.setdefault("backend", backend)
    return _truncate(data)


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (``.tmp`` + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.replace(tmp, path)


def _walk(root: Path, *, include_deps: bool) -> list[Path]:
    """List project files, skipping dot-directories and build output."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if (name == ".deps" and include_deps)
            or (not name.startswith(".") and name not in _SKIP_DIRS)
        ]
        for name in filenames:
            files.append(Path(dirpath) / name)
    return sorted(files)


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _lua_outline(text: str) -> dict:
    """Extract a script's declared properties, callbacks and literals."""
    return {
        "properties": _PROPERTY_RE.findall(text),
        "functions": _FUNCTION_RE.findall(text),
        "requires": _REQUIRE_RE.findall(text),
        "messages": _MSG_POST_RE.findall(text),
        "hashes": _HASH_RE.findall(text),
    }


def _engine_port(root: Path) -> int | None:
    entry = _RUNNING.get(str(root))
    return entry["port"] if entry else None


def _editor_dialect(op: object) -> bool:
    """Whether ``op`` uses the Editor transaction bridge's payload dialect.

    The registry and the Editor bridge share some op *names* but not their
    payloads: the registry's ``set_property`` carries
    ``parent``/``key``/``value``, whereas the Editor's carries
    ``node``/``key``/``value``. Routing on the name alone would send a
    registry-dialect batch to ``transact_mutations``, which raises where the
    AST path would have succeeded.
    """
    if not isinstance(op, dict):
        return False
    name = op.get("op")
    if name == "add_resource":
        return isinstance(op.get("path"), str) and isinstance(
            op.get("template"), str
        )
    if name == "set_property":
        return isinstance(op.get("node"), str)
    return False


def _slice_ring(
    entry: dict, offset: int, limit: int, filter: str | None
) -> dict:
    """Slice a spawned process's log ring like ``TcpLogCollector.slice``.

    Offsets are absolute over every line ever received, so ring eviction
    never shifts them; the window is taken *before* the filter is applied,
    so a filtered read never skips lines on the next page.
    """
    with entry["lock"]:
        total = entry["total"]
        ring = list(entry["ring"])
    oldest = total - len(ring)
    start = max(offset, oldest)
    window = ring[start - oldest : start - oldest + limit]
    next_offset = start + len(window)
    if filter is not None:
        needle = filter.lower()
        window = [line for line in window if needle in line.lower()]
    return {"lines": window, "next_offset": next_offset}


def _stop_entry(root: Path) -> dict | None:
    """Terminate and deregister the engine (and TCP collector) for ``root``."""
    entry = _RUNNING.pop(str(root), None)
    collector = _TCP_COLLECTORS.pop(str(root), None)
    if collector is not None:
        collector.stop()
    if entry is None:
        return None

    process = entry["process"]
    port = entry["port"]
    if port is not None:
        try:
            engine_client.EngineClient(port).exit(0)
        except Exception:  # pragma: no cover - engine already gone
            logger.debug("engine exit POST failed; falling back to SIGTERM")
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - stubborn process
        process.kill()
        process.wait(timeout=5)
    thread = entry.get("thread")
    if thread is not None:
        # The pump closes the stdout pipe as it hits EOF; join it so the
        # descriptor is released before this call returns.
        thread.join(timeout=5)
    entry["exit_code"] = process.returncode
    return entry


def _attach_tcp_collector(
    port: int,
) -> engine_client.TcpLogCollector | None:
    """Connect a log collector when the engine advertises a ``log_port``.

    Best effort: an unreachable ``/info`` or log stream leaves the run
    intact and simply yields no TCP log source.
    """
    try:
        info = engine_client.EngineClient(port).info()
    except Exception:
        logger.debug("engine /info unavailable; no TCP log collector attached")
        return None
    log_port = info.get("log_port")
    if not isinstance(log_port, int):
        return None
    collector = engine_client.TcpLogCollector("127.0.0.1", log_port)
    try:
        collector.connect(timeout=2.0)
    except Exception:
        logger.debug("engine TCP log stream unavailable on port %s", log_port)
        return None
    return collector


def _require_engine(root: Path) -> int:
    port = _engine_port(root)
    if port is None:
        raise RuntimeError(
            f"no running engine is registered for {root}; call defold_run first"
        )
    return port


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def defold_doctor(project_root: str | None = None) -> str:
    """Report the host and project health: Java, cached artifacts, backend and dependencies."""
    root = _try_resolve_root(project_root)
    java = toolchain.check_java()
    installations = toolchain.read_installations()

    report: dict = {
        "ok": True,
        "project_root": str(root) if root else None,
        "java": java,
        "installations": installations,
        "cache_dir": str(get_cache_dir()),
    }

    if root is not None:
        project = GameProject.load(root)
        declared = project.get_dependencies()
        deps_dir = root / ".deps"
        unpacked = (
            sorted(
                entry.name
                for entry in deps_dir.iterdir()
                if entry.is_dir() and not entry.name.startswith(".")
            )
            if deps_dir.is_dir()
            else []
        )
        report["backend"] = _backend(root)
        report["dependencies"] = {
            "declared": declared,
            "unpacked": unpacked,
            "missing": [
                url for url in declared if not _dependency_present(url, unpacked)
            ],
        }

    db_path = get_cache_dir() / "docs.db"
    if db_path.is_file():
        index = docs_index.DocsIndex(str(db_path))
        try:
            report["docs"] = index.stats()
        finally:
            index.close()
    else:
        report["docs"] = None

    return _respond(report, backend=report.get("backend"))


def _dependency_present(url: str, unpacked: list[str]) -> bool:
    """Whether a dependency URL has a plausible unpacked directory."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    name = name.removesuffix(".zip")
    for candidate in (name, name.split("-", 1)[0]):
        if candidate and candidate in unpacked:
            return True
    return False


def defold_setup_toolchain(
    spec: str = DEFAULT_DEFOLD_VERSION,
    install_jdk25: bool = False,
    project_root: str | None = None,
) -> str:
    """Download and cache the Defold toolchain for a channel or pinned version."""
    result = toolchain.ensure_toolchain(spec, install_jdk25=install_jdk25)
    db_path = get_cache_dir() / "docs.db"
    docs: dict | None = None
    if _docs_db_needs_build(db_path):
        docs = _build_docs_db(spec)
    return _respond({"ok": True, "spec": spec, "docs": docs, **result})


def _is_sqlite_database(db_path: Path) -> bool:
    """Whether ``db_path`` is an openable SQLite database.

    The file is probed with a direct connection rather than through
    :class:`~defold_mcp.docs_index.DocsIndex`: that constructor runs DDL and
    cannot close a connection it never returns, so a malformed file would
    leave one open (a ``ResourceWarning`` on finalisation).
    """
    try:
        connection = sqlite3.connect(db_path)
    except sqlite3.Error:
        return False
    try:
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError:
        return False
    finally:
        connection.close()
    return True


def _docs_db_needs_build(db_path: Path) -> bool:
    """Whether the offline documentation index must be (re)built.

    A missing index needs building; so does one that exists but holds no
    documents — an interrupted ingest leaves an empty FTS table behind,
    and treating that as "already built" would make every later search
    return nothing, permanently. A file that is not a usable SQLite
    database (garbage or truncated bytes) also needs rebuilding, rather
    than raising out of the setup call.
    """
    if not db_path.is_file():
        return True
    if not _is_sqlite_database(db_path):
        return True
    index = docs_index.DocsIndex(str(db_path))
    try:
        return not index.stats()
    except sqlite3.DatabaseError:
        return True
    finally:
        index.close()


def _discard_unusable_db(db_path: Path) -> None:
    """Remove ``db_path`` when it exists but is not an openable database.

    :func:`_docs_db_needs_build` reports such a file as needing a rebuild;
    the rebuild itself can only start once the unusable file is gone.
    """
    if db_path.is_file() and not _is_sqlite_database(db_path):
        db_path.unlink(missing_ok=True)


def _build_docs_db(spec: str) -> dict:
    """Build the FTS5 documentation index from the network (live only)."""
    _discard_unusable_db(get_cache_dir() / "docs.db")
    index = docs_index.DocsIndex(str(get_cache_dir() / "docs.db"))
    try:
        counts: dict[str, Any] = {}
        counts["llms"] = index.ingest_llms_tarball(index.fetch_llms_tarball())
        counts["assets"] = index.ingest_assets(index.fetch_asset_store())
        resolved = toolchain.resolve_version(spec)
        sha1 = resolved.get("sha1")
        if sha1:
            counts["refdoc"] = index.ingest_refdoc(index.fetch_refdoc_zip(sha1))
        counts["stats"] = index.stats()
        return counts
    finally:
        index.close()


def defold_project_init(
    project_root: str,
    title: str = "My Game",
    profile: str = "2d_pixel",
    dependencies: list[str] | None = None,
) -> str:
    """Scaffold a new Defold project from a verified profile, with licence notice and ignore files."""
    result = templates.render_project(
        project_root,
        title=title,
        profile=profile,
        dependencies=dependencies,
    )
    root = Path(result["root"])

    # Re-apply the requested dependencies through GameProject so the
    # indexed ``dependencies#N`` keys are rewritten canonically.
    project = GameProject.load(root)
    added: list[str] = []
    for dependency in dependencies or []:
        url = templates.KNOWN_LIBRARY_URLS.get(dependency, dependency)
        if project.add_dependency(url):
            added.append(url)
    project.save()

    return _respond(
        {
            "ok": True,
            "project_root": str(root),
            "profile": profile,
            "title": title,
            "files": result["files"],
            "dependencies_added": added,
        },
        backend=_backend(root),
    )


def defold_project_info(project_root: str | None = None) -> str:
    """Summarise ``game.project``: title, bootstrap, display/physics settings and resource inventory."""
    root = _resolve_root(project_root)
    project = GameProject.load(root)

    resources = [
        {"path": _relative(root, path), "extension": path.suffix}
        for path in _walk(root, include_deps=False)
        if path.suffix.lower() in _DOC_EXTENSIONS
    ]

    deps_dir = root / ".deps"
    deps_folders = (
        sorted(
            entry.name
            for entry in deps_dir.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        )
        if deps_dir.is_dir()
        else []
    )

    return _respond(
        {
            "ok": True,
            "project_root": str(root),
            "title": (project.get("project", "title") or "").strip('"'),
            "bootstrap": {
                "main_collection": project.get(
                    "bootstrap", "main_collection"
                ),
                "render": project.get("bootstrap", "render"),
                "debug_init_script": project.get(
                    "bootstrap", "debug_init_script"
                ),
            },
            "display": {
                "width": project.get("display", "width"),
                "height": project.get("display", "height"),
            },
            "physics": {
                "type": project.get("physics", "type"),
                "scale": project.get("physics", "scale"),
                "gravity_y": project.get("physics", "gravity_y"),
            },
            "sections": project.sections(),
            "dependencies": project.get_dependencies(),
            "deps_folders": deps_folders,
            "resources": resources,
        },
        backend=_backend(root),
    )


def defold_project_settings(
    action: str,
    section: str,
    key: str,
    value: str | None = None,
    project_root: str | None = None,
) -> str:
    """Read, write or delete one ``game.project`` setting, preserving comments and formatting."""
    root = _resolve_root(project_root)
    project = GameProject.load(root)
    result: dict = {"ok": True, "action": action, "section": section, "key": key}

    if action == "get":
        result["value"] = project.get(section, key)
    elif action == "set":
        if value is None:
            raise ValueError("action 'set' requires a value")
        project.set(section, key, value)
        project.save()
        result["value"] = project.get(section, key)
    elif action == "delete":
        project.delete(section, key)
        project.save()
    else:
        raise ValueError(
            f"unknown action {action!r}; expected 'get', 'set' or 'delete'"
        )

    result["editor_sync"] = False
    return _respond(result, backend=_backend(root))


def defold_sync_deps(
    action: str,
    url: str | None = None,
    project_root: str | None = None,
    spec: str = DEFAULT_DEFOLD_VERSION,
) -> str:
    """List, add, remove or sync the project's ``.deps`` archive dependencies."""
    root = _resolve_root(project_root)
    project = GameProject.load(root)
    result: dict = {"ok": True, "action": action, "project_root": str(root)}

    if action == "list":
        result["dependencies"] = project.get_dependencies()
    elif action == "add":
        if url is None:
            raise ValueError("action 'add' requires a url")
        resolved = templates.KNOWN_LIBRARY_URLS.get(url, url)
        result["added"] = project.add_dependency(resolved)
        project.save()
        result["dependencies"] = project.get_dependencies()
    elif action == "remove":
        if url is None:
            raise ValueError("action 'remove' requires a url")
        resolved = templates.KNOWN_LIBRARY_URLS.get(url, url)
        result["removed"] = project.remove_dependency(resolved)
        project.save()
        result["dependencies"] = project.get_dependencies()
    elif action == "sync":
        result.update(deps.sync_project_deps(root, spec=spec))
    else:
        raise ValueError(
            f"unknown action {action!r}; expected 'list', 'add', 'remove' or 'sync'"
        )

    return _respond(result, backend=_backend(root))


def defold_list_resources(
    project_root: str | None = None,
    extension: str | None = None,
    glob_pattern: str | None = None,
    include_deps: bool = False,
) -> str:
    """List project resources, optionally filtered by extension or a glob pattern."""
    root = _resolve_root(project_root)
    wanted = extension if extension is None or extension.startswith(".") else f".{extension}"

    resources = []
    for path in _walk(root, include_deps=include_deps):
        relative = _relative(root, path)
        if wanted is not None and path.suffix.lower() != wanted.lower():
            continue
        if glob_pattern is not None and not fnmatch.fnmatch(relative, glob_pattern):
            continue
        resources.append({"path": relative, "extension": path.suffix})

    return _respond(
        {
            "ok": True,
            "project_root": str(root),
            "count": len(resources),
            "resources": resources,
        },
        backend=_backend(root),
    )


def defold_inspect_resource(
    resource_path: str, project_root: str | None = None
) -> str:
    """Parse a resource into clean JSON, or outline a Lua script's properties and callbacks."""
    root = _resolve_root(project_root)
    path = _resource_path(root, resource_path)
    if not path.is_file():
        raise FileNotFoundError(f"no such resource: {resource_path}")

    text = path.read_text(encoding="utf-8")
    result: dict = {
        "ok": True,
        "path": resource_path,
        "extension": path.suffix,
    }
    if path.suffix.lower() in _LUA_SOURCE_SUFFIXES:
        result["outline"] = _lua_outline(text)
    else:
        result["tree"] = decode_embedded_data(
            parse_textproto(text, schema=ALWAYS_LIST_FIELDS)
        )

    return _respond(result, backend=_backend(root))


def defold_mutate_resource(
    resource_path: str,
    operations: list[dict],
    project_root: str | None = None,
) -> str:
    """Apply a batch of structured mutations to a resource and write it back atomically."""
    root = _resolve_root(project_root)
    path = _resource_path(root, resource_path)
    extension = path.suffix

    if path.is_file():
        tree = decode_embedded_data(
            parse_textproto(
                path.read_text(encoding="utf-8"), schema=ALWAYS_LIST_FIELDS
            )
        )
    else:
        tree = {}

    editor = editor_client.probe_editor(root)
    served_by = "headless"
    if editor is not None and operations and all(
        _editor_dialect(op) for op in operations
    ):
        editor.transact_mutations(operations, resource_path)
        served_by = "editor"
    else:
        new_tree = apply_operations(tree, operations, extension=extension)
        _atomic_write(path, serialize_textproto(new_tree, extension=extension))

    return _respond(
        {
            "ok": True,
            "resource_path": resource_path,
            "operations": len(operations),
            "bytes_written": path.stat().st_size if path.is_file() else 0,
        },
        backend=served_by,
    )


def defold_compile(
    project_root: str | None = None, variant: str = "debug"
) -> str:
    """Compile the project, through the Editor when it is open and headlessly otherwise."""
    root = _resolve_root(project_root)
    editor = editor_client.probe_editor(root)

    started = time.monotonic()
    tail: dict = {}
    if editor is not None:
        outcome = editor.compile()
        diagnostics = outcome.get("issues", [])
        ok = bool(outcome.get("ok"))
        backend = "editor"
    else:
        result = bob_runner.run_bob(root, ["build"], variant=variant)
        diagnostics = result.diagnostics
        ok = result.ok
        backend = "headless"
        tail = _failure_tail(result)

    return _respond(
        {
            "ok": ok,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "diagnostics": diagnostics,
            **tail,
        },
        backend=backend,
    )


def _failure_tail(result: "bob_runner.BobResult") -> dict:
    """The raw bob output tail, but only when the run failed.

    A hard ``SEVERE`` failure can leave ``diagnostics`` empty, so the raw
    output is the only thing an agent consumer can self-correct from; a
    successful run keeps its response small.
    """
    if result.ok:
        return {}
    return {"stdout_tail": result.stdout[-_DIAGNOSTIC_TAIL_CHARS:]}


def defold_bundle(
    platform: str,
    project_root: str | None = None,
    variant: str = "release",
    bundle_format: str | None = None,
    architectures: str | None = None,
) -> str:
    """Bundle the project for a platform and list the artifacts it produced."""
    root = _resolve_root(project_root)
    editor = editor_client.probe_editor(root)

    if editor is not None:
        outcome = editor.bob_call(
            {"platform": platform, "archive": True},
            ["resolve", "build", "bundle"],
        )
        return _respond(
            {"ok": True, "result": outcome, "artifacts": _build_artifacts(root)},
            backend="editor",
        )

    report_path = root / "build-report.json"
    result = bob_runner.run_bob(
        root,
        ["build", "bundle"],
        platform=platform,
        variant=variant,
        build_report_path=report_path,
        bundle_format=bundle_format,
        architectures=architectures,
    )
    return _respond(
        {
            "ok": result.ok,
            "exit_code": result.exit_code,
            "diagnostics": result.diagnostics,
            "build_report": result.build_report,
            "artifacts": _build_artifacts(root),
            **_failure_tail(result),
        },
        backend="headless",
    )


def _build_artifacts(root: Path) -> list[dict]:
    build_dir = root / "build"
    if not build_dir.is_dir():
        return []
    return [
        {"path": _relative(root, path), "size": path.stat().st_size}
        for path in sorted(build_dir.rglob("*"))
        if path.is_file()
    ]


def defold_preview(
    resource_path: str,
    width: int = 1280,
    height: int = 720,
    project_root: str | None = None,
) -> str:
    """Render a static PNG preview of a resource; requires the Defold Editor."""
    root = _resolve_root(project_root)
    editor = editor_client.probe_editor(root)
    if editor is None:
        return _respond(
            {
                "ok": False,
                "error": "editor not running",
                "hint": "defold_preview requires an open Defold Editor",
            },
            backend="headless",
        )

    out_path = editor.render_preview(resource_path, width, height)
    return _respond(
        {
            "ok": True,
            "resource_path": resource_path,
            "preview_path": str(out_path),
            "width": width,
            "height": height,
        },
        backend="editor",
    )


def defold_run(
    project_root: str | None = None,
    config_overrides: dict | None = None,
    xvfb: bool = False,
    compile_first: bool = True,
    spec: str = DEFAULT_DEFOLD_VERSION,
) -> str:
    """Launch the game, through the Editor when open and as a managed headless process otherwise."""
    root = _resolve_root(project_root)
    editor = editor_client.probe_editor(root)
    if editor is not None:
        editor.run(focus=False)
        return _respond({"ok": True, "pid": None, "engine_port": None}, backend="editor")

    # A second run must not orphan the first engine: stop any live process
    # (and its log collector) registered for this root before spawning.
    if _stop_entry(root) is not None:
        logger.info("replaced the engine already running for %s", root)

    if compile_first:
        compile_result = bob_runner.run_bob(
            root, ["build"], variant="debug", spec=spec
        )
        if not compile_result.ok:
            return _respond(
                {
                    "ok": False,
                    "diagnostics": compile_result.diagnostics,
                    "engine_port": None,
                    "pid": None,
                },
                backend="headless",
            )

    toolchain_result = toolchain.ensure_toolchain(
        spec, include_editor_binaries=True
    )
    engine = toolchain_result.get("dmengine")
    if not engine or not Path(engine).is_file():
        raise FileNotFoundError(
            "dmengine not found in the cache; run defold_setup_toolchain first"
        )

    argv = [str(engine)]
    for key, value in (config_overrides or {}).items():
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        argv.append(f"--config={key}={rendered}")
    if xvfb and shutil.which("xvfb-run"):
        argv = ["xvfb-run", "-a", *argv]

    process = subprocess.Popen(
        argv,
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    ring: deque[str] = deque(maxlen=_RING_CAPACITY)
    entry = {
        "process": process,
        "ring": ring,
        "port": None,
        "total": 0,
        "lock": threading.Lock(),
    }
    _RUNNING[str(root)] = entry

    def _pump() -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                with entry["lock"]:
                    ring.append(line.rstrip("\n"))
                    entry["total"] += 1
                if entry["port"] is None:
                    port = engine_client.discover_engine_port(line)
                    if port is not None:
                        entry["port"] = port
        finally:
            # Close the pipe on the reading thread so the descriptor is
            # released deterministically once the engine exits.
            process.stdout.close()

    entry["thread"] = threading.Thread(target=_pump, daemon=True)
    entry["thread"].start()

    deadline = time.monotonic() + 10.0
    while entry["port"] is None and time.monotonic() < deadline:
        if process.poll() is not None:
            break
        time.sleep(0.05)

    if entry["port"] is not None:
        collector = _attach_tcp_collector(entry["port"])
        if collector is not None:
            _TCP_COLLECTORS[str(root)] = collector

    return _respond(
        {
            "ok": True,
            "pid": process.pid,
            "engine_port": entry["port"],
        },
        backend="headless",
    )


def defold_stop(project_root: str | None = None) -> str:
    """Stop the running game cleanly and return the trailing log lines."""
    root = _resolve_root(project_root)
    entry = _stop_entry(root)
    if entry is None:
        # Nothing was probed, so no backend is claimed.
        return _respond({"ok": False, "error": "no running game"})

    return _respond(
        {
            "ok": True,
            "exit_code": entry["exit_code"],
            "tail": list(entry["ring"])[-20:],
        },
        backend="headless",
    )


def defold_hot_reload(
    resources: list[str], project_root: str | None = None
) -> str:
    """Hot-swap modified resources into the running debug engine."""
    root = _resolve_root(project_root)
    editor = editor_client.probe_editor(root)
    if editor is not None:
        return _respond(
            {"ok": True, "result": editor.hot_reload()}, backend="editor"
        )

    build = bob_runner.run_bob(root, ["build"], variant="debug")
    if not build.ok:
        return _respond(
            {"ok": False, "diagnostics": build.diagnostics}, backend="headless"
        )

    client = engine_client.EngineClient(_require_engine(root))
    chunks = client.resource_reload(resources)
    return _respond(
        {"ok": True, "chunks": chunks, "resources": len(resources)},
        backend="headless",
    )


def defold_logs(
    source: str = "auto",
    offset: int = 0,
    limit: int = 100,
    filter: str | None = None,
    project_root: str | None = None,
) -> str:
    """Read incremental logs from the Editor, a spawned process or the engine TCP stream."""
    root = _resolve_root(project_root)
    chosen = source
    if chosen == "auto":
        if str(root) in _RUNNING:
            chosen = "process"
        elif str(root) in _TCP_COLLECTORS:
            chosen = "tcp"
        else:
            chosen = "editor"

    if chosen == "process":
        entry = _RUNNING.get(str(root))
        if entry is None:
            raise RuntimeError("no spawned engine is registered for this project")
        result = _slice_ring(entry, offset, limit, filter)
        return _respond(
            {"ok": True, "source": "process", **result},
            backend=_backend(root),
        )

    if chosen == "tcp":
        collector = _TCP_COLLECTORS.get(str(root))
        if collector is None:
            raise RuntimeError("no TCP log collector is registered for this project")
        result = collector.slice(offset, limit, filter)
        return _respond(
            {"ok": True, "source": "tcp", **result}, backend=_backend(root)
        )

    if chosen == "editor":
        editor = editor_client.probe_editor(root)
        if editor is None:
            raise RuntimeError("editor not running and no engine log source available")
        console = editor.console()
        lines = console.get("lines", [])
        return _respond(
            {"ok": True, "source": "editor", "lines": lines},
            backend="editor",
        )

    raise ValueError(
        f"unknown source {source!r}; expected 'auto', 'editor', 'process' or 'tcp'"
    )


def defold_test_run(
    test_collection: str,
    project_root: str | None = None,
    timeout_s: float = 60.0,
) -> str:
    """Build a test collection as the entry point, run it, and summarise the results."""
    root = _resolve_root(project_root)
    build = test_runner.build_test_collection(root, test_collection)
    if not build.ok:
        # The collection never became the archive's bootstrap, so running
        # the engine would exit 1 with no diagnostics: report the build.
        return _respond(
            {
                "ok": False,
                "error": "the test collection could not be built",
                "exit_code": build.exit_code,
                "diagnostics": build.diagnostics,
                "stdout_tail": build.stdout[-_DIAGNOSTIC_TAIL_CHARS:],
            },
            backend="headless",
        )

    summary = test_runner.run_headless_tests(
        root, test_collection, timeout_s=timeout_s
    )
    return _respond(
        {
            "ok": summary["ok"],
            "run": summary["run"],
            "status": summary["status"],
            "passed": summary["passed"],
            "failed": summary["failed"],
            "exit_code": summary["exit_code"],
            "rejected_events": summary["rejected_events"],
            "tests": summary["tests"],
            "stdout_tail": summary.get("stdout_tail", ""),
        },
        backend="headless",
    )


def defold_runtime_inspect(
    action: str = "info",
    query: str | None = None,
    name: str | None = None,
    automation_id: str | None = None,
    project_root: str | None = None,
) -> str:
    """Inspect a live debug engine through the Engine Service or the Automation Bridge."""
    root = _resolve_root(project_root)
    port = _require_engine(root)

    if action in ("info", "state"):
        client = engine_client.EngineClient(port)
        payload = client.info() if action == "info" else client.state()
        return _respond({"ok": True, "action": action, **payload}, backend="headless")

    if action in ("scene", "elements", "element", "screen"):
        bridge = bridge_client.BridgeClient(port)
        if action == "scene":
            payload = bridge.scene()
        elif action == "elements":
            payload = bridge.elements(
                type=query, name=name, automation_id=automation_id
            )
        elif action == "element":
            matches = bridge.elements(
                name=name, automation_id=automation_id, limit=1
            )
            payload = matches[0] if matches else None
        else:
            payload = bridge.health().get("screen")
        return _respond(
            {"ok": True, "action": action, "result": payload}, backend="headless"
        )

    raise ValueError(
        f"unknown action {action!r}; expected info, state, scene, elements, "
        "element or screen"
    )


def defold_runtime_act(
    action: str,
    x: float | None = None,
    y: float | None = None,
    automation_id: str | None = None,
    key: str | None = None,
    dx: float | None = None,
    dy: float | None = None,
    points: list | None = None,
    out_path: str | None = None,
    command: str | None = None,
    value: int | None = None,
    project_root: str | None = None,
    poll_timeout: float = 5.0,
) -> str:
    """Drive a live debug engine: gestures, keys, screenshots, recording and system commands."""
    root = _resolve_root(project_root)
    port = _require_engine(root)

    if action == "system_command":
        client = engine_client.EngineClient(port)
        response = _system_command(client, command, value)
        return _respond(
            {
                "ok": response is not None,
                "action": action,
                "command": command,
                "status": getattr(response, "status_code", None),
            },
            backend="headless",
        )

    if action in ("record_start", "record_stop"):
        recorder = engine_client.RecordingClient(engine_client.EngineClient(port))
        if action == "record_start":
            operation_id = recorder.start(fps=value or 30)
            return _respond(
                {"ok": True, "action": action, "operation_id": operation_id},
                backend="headless",
            )
        if out_path is None:
            raise ValueError("record_stop requires out_path")
        receipt = recorder.stop_and_fetch(
            out_path, timeout=poll_timeout
        )
        return _respond(
            {"ok": True, "action": action, **receipt}, backend="headless"
        )

    bridge = bridge_client.BridgeClient(port)
    if action == "click":
        result = bridge.click(
            x=x, y=y, automation_id=automation_id, poll_timeout=poll_timeout
        )
    elif action == "drag":
        result = bridge.drag(
            from_point=(x or 0.0, y or 0.0),
            to_point=(dx or 0.0, dy or 0.0),
            automation_id=automation_id,
            poll_timeout=poll_timeout,
        )
    elif action == "drag_path":
        result = bridge.drag_path(
            points=[tuple(point) for point in (points or [])],
            automation_id=automation_id,
            poll_timeout=poll_timeout,
        )
    elif action == "key":
        if key is None:
            raise ValueError("key action requires a key")
        result = bridge.key(key=key, automation_id=automation_id, poll_timeout=poll_timeout)
    elif action == "wheel":
        result = bridge.wheel(
            delta_x=dx or 0.0,
            delta_y=dy or 0.0,
            automation_id=automation_id,
            poll_timeout=poll_timeout,
        )
    elif action == "screenshot":
        result = bridge.take_screenshot(poll_timeout=poll_timeout)
    else:
        raise ValueError(
            f"unknown action {action!r}; expected click, drag, drag_path, key, "
            "wheel, screenshot, record_start, record_stop or system_command"
        )

    return _respond(
        {"ok": True, "action": action, "result": result}, backend="headless"
    )


def _system_command(client: engine_client.EngineClient, command: str | None, value: int | None):
    """Map a ``system_command`` name onto the Engine Service ``@system`` POSTs."""
    if command == "toggle_profile":
        return client.toggle_profile()
    if command == "toggle_physics_debug":
        return client.toggle_physics_debug()
    if command == "set_update_frequency":
        if value is None:
            raise ValueError("set_update_frequency requires a value")
        return client.set_update_frequency(value)
    if command == "set_vsync":
        if value is None:
            raise ValueError("set_vsync requires a value")
        return client.set_vsync(value)
    if command == "reboot":
        return client.reboot()
    if command == "exit":
        return client.exit(value or 0)
    raise ValueError(
        f"unknown system command {command!r}; expected toggle_profile, "
        "toggle_physics_debug, set_update_frequency, set_vsync, reboot or exit"
    )


def defold_docs_search(
    action: str = "search",
    query: str = "",
    scope: str = "all",
    doc_id: str | None = None,
    offset: int = 0,
    limit: int = 25000,
    project_root: str | None = None,
) -> str:
    """Search the offline FTS5 documentation index or read one indexed document."""
    index = docs_index.DocsIndex(str(get_cache_dir() / "docs.db"))
    try:
        if action == "search":
            scopes = docs_index.VALID_SCOPES if scope == "all" else (scope,)
            results: list[dict] = []
            for candidate in scopes:
                if candidate == "all":
                    continue
                results.extend(index.search(query, candidate, limit=limit))
            root = _try_resolve_root(project_root)
            backend = _backend(root) if root is not None else None
            return _respond(
                {
                    "ok": True,
                    "action": "search",
                    "query": query,
                    "scope": scope,
                    "results": results,
                    # The offline index always answers; the live Editor /ref
                    # route is only noted, never called.
                    "editor_ref_available": backend == "editor",
                },
                backend=backend,
            )
        if action == "get":
            if doc_id is None:
                raise ValueError("action 'get' requires a doc_id")
            return _respond(
                {"ok": True, "action": "get", **index.get_doc(doc_id, offset, limit)}
            )
        raise ValueError(f"unknown action {action!r}; expected 'search' or 'get'")
    finally:
        index.close()


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------

_INSTRUCTIONS = (
    "Dual-mode server for the Defold game engine. Use defold_doctor and "
    "defold_project_info to orient yourself, defold_sync_deps to unpack "
    "dependencies, defold_mutate_resource for structured resources, and the "
    "compile/bundle/test tools to verify your work. Never hand-edit "
    "double-escaped data: strings in .collection or .go files."
)


def build_server() -> MCPServer:
    """Build the ``defold`` MCP server with all twenty tools registered."""
    server = MCPServer(name="defold", instructions=_INSTRUCTIONS)

    for tool in (
        defold_doctor,
        defold_setup_toolchain,
        defold_project_init,
        defold_project_info,
        defold_project_settings,
        defold_sync_deps,
        defold_list_resources,
        defold_inspect_resource,
        defold_mutate_resource,
        defold_compile,
        defold_bundle,
        defold_preview,
        defold_run,
        defold_stop,
        defold_hot_reload,
        defold_logs,
        defold_test_run,
        defold_runtime_inspect,
        defold_runtime_act,
        defold_docs_search,
    ):
        server.add_tool(tool)

    return server
