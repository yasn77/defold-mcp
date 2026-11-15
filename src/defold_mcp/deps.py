"""Synchronisation of a project's ``.deps/`` directory.

Downloads dependency zips declared in ``game.project`` plus the engine
``builtins.zip``, unpacks them into ``<project_root>/.deps/`` with
zip-slip protection, and keeps ``.defignore``/``.gitignore`` in sync.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import zipfile
from pathlib import Path

import httpx

from . import config, toolchain
from .project import GameProject

# Lines that must exist in the ignore files.
_DEPS_IGNORE_LINE = "/.deps"
_BUILTINS_IGNORE_LINE = "/builtins"


def extract_zip(
    zip_bytes: bytes,
    dest: Path,
    *,
    strip_root: str | None = None,
) -> list[Path]:
    """Unpack ``zip_bytes`` into ``dest`` with zip-slip protection.

    Every member's resolved path must stay inside ``dest``; entries with
    ``..`` components or absolute paths raise ``ValueError`` naming the
    offending entry, and nothing is written before all names are
    validated. When ``strip_root`` is set and every non-empty entry
    shares that exact top-level directory, the component is stripped
    (Defold dependency and builtins zips commonly wrap their contents
    in a single directory matching the target name). Directory entries
    create directories; file entries stream through ``.tmp`` then
    ``os.replace``. Returns the list of written file paths.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        members = zf.infolist()
        names = [m.filename for m in members]

        prefix = ""
        if strip_root and names:
            roots = {n.split("/", 1)[0] for n in names if n}
            if roots == {strip_root}:
                prefix = strip_root + "/"

        # Validate every member before writing anything.
        for name in names:
            if not name:
                continue
            if name.startswith(("/", "\\")) or ".." in name.split("/"):
                raise ValueError(
                    f"Refusing to extract unsafe zip entry {name!r} "
                    f"(path escapes {dest})."
                )
            target = (dest / (name[len(prefix):] if name.startswith(prefix) else name))
            resolved = target.resolve()
            if not resolved.is_relative_to(dest.resolve()):
                raise ValueError(
                    f"Refusing to extract unsafe zip entry {name!r} "
                    f"(path escapes {dest})."
                )

        written: list[Path] = []
        for member in members:
            name = member.filename
            if not name:
                continue
            rel = name[len(prefix):] if name.startswith(prefix) else name
            if not rel:
                continue
            target = dest / rel
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            with zf.open(member) as src, tmp.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(tmp, target)
            written.append(target)
    return written


def _dependency_name(url: str) -> str:
    """Derive a sanitised directory name from a dependency URL.

    The last path segment minus a ``.zip`` suffix; falls back to a short
    hash of the URL when the segment is unusable (empty, ``.``/``..``, or
    containing a path separator).
    """
    segment = url.rstrip("/").rsplit("/", 1)[-1]
    if segment.lower().endswith(".zip"):
        segment = segment[:-4]
    segment = segment.strip()
    if (
        segment
        and segment not in (".", "..")
        and "/" not in segment
        and "\\" not in segment
    ):
        return segment
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _ensure_ignore_line(path: Path, line: str) -> bool:
    """Ensure ``line`` exists in ``path`` (created if missing).

    Returns True when the file was modified.
    """
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        lines = existing.splitlines()
        if line in lines:
            return False
        new_text = existing
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += line + "\n"
    else:
        new_text = line + "\n"
    path.write_text(new_text, encoding="utf-8")
    return True


def _fix_bare_builtins_line(path: Path) -> bool:
    """Rewrite a bare ``builtins`` line in ``path`` to ``/builtins``.

    Only the exact bare form (no leading slash) is rewritten; a
    pre-existing ``/builtins`` line is left alone, keeping the fix
    idempotent. Returns True when the file was modified.
    """
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    changed = False
    for index, line in enumerate(lines):
        if line == "builtins":
            lines[index] = _BUILTINS_IGNORE_LINE
            changed = True
    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def sync_project_deps(
    project_root: Path | str,
    *,
    client: httpx.Client | None = None,
    spec: str = "stable",
    force: bool = False,
) -> dict:
    """Sync ``<project_root>/.deps/`` with the project's dependencies.

    1. Load ``game.project`` and read the dependency archive URLs.
    2. Download and unpack each zip into ``.deps/<sanitised-name>/``
       (zip-slip protected; a single common top-level directory inside
       the zip is stripped).
    3. Download and unpack ``builtins.zip`` into ``.deps/builtins/``.
    4. Ensure ``/.deps`` is present in ``.defignore`` and ``.gitignore``,
       and rewrite a bare ``builtins`` line in ``.gitignore`` to
       ``/builtins``.

    Returns ``{"dependencies": [names...], "builtins": True,
    "ignore_fixed": bool}``.
    """
    project_root = Path(project_root)
    project = GameProject.load(project_root)
    dependency_urls = project.get_dependencies()

    deps_dir = project_root / ".deps"
    names: list[str] = []
    for url in dependency_urls:
        name = _dependency_name(url)
        archive = deps_dir / f".{name}.zip.tmp"
        try:
            toolchain.download_artifact(
                url, archive, client=client, expected_magic=b"PK"
            )
            extract_zip(
                archive.read_bytes(),
                deps_dir / name,
                strip_root=name,
            )
        finally:
            archive.unlink(missing_ok=True)
        names.append(name)

    resolved = toolchain.resolve_version(spec, client=client, force=force)
    sha1 = resolved["sha1"]
    if sha1 is not None:
        builtins_url = (
            f"{config.D_DEFOLD_BASE}/archive/{sha1}/engine/share/builtins.zip"
        )
    else:
        builtins_url = (
            f"{config.GITHUB_RELEASES_BASE}/{resolved['version']}/builtins.zip"
        )
    builtins_archive = deps_dir / ".builtins.zip.tmp"
    try:
        toolchain.download_artifact(
            builtins_url,
            builtins_archive,
            client=client,
            expected_magic=b"PK",
        )
        extract_zip(
            builtins_archive.read_bytes(),
            deps_dir / "builtins",
            strip_root="builtins",
        )
    finally:
        builtins_archive.unlink(missing_ok=True)

    ignore_fixed = False
    ignore_fixed |= _ensure_ignore_line(
        project_root / ".defignore", _DEPS_IGNORE_LINE
    )
    ignore_fixed |= _fix_bare_builtins_line(project_root / ".gitignore")
    ignore_fixed |= _ensure_ignore_line(
        project_root / ".gitignore", _DEPS_IGNORE_LINE
    )

    return {"dependencies": names, "builtins": True, "ignore_fixed": ignore_fixed}
