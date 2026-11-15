"""Locating and installing the portable ``defold`` agent skill.

The skill has one source of truth: the repository tree at ``skill/defold``.
The wheel force-includes that tree at ``defold_mcp/skill/defold`` (see
``pyproject.toml``), so an installed package — pip, ``uvx`` or the container —
carries its own copy. Installed runs resolve the bundled copy; development runs
fall back to the repository tree.

Installation always copies. The bundled copy lives in ``site-packages`` and a
symlink into it would break on the next upgrade, so the destination is a plain
directory tree the harness owns.
"""

from __future__ import annotations

import os
import shutil
from importlib import resources
from pathlib import Path

from defold_mcp.integrations import resolve_template

PACKAGE = "defold_mcp"
SKILL_DIR_NAME = "defold"

#: ``(label, install path, marker directory)``. A harness counts as detected
#: when its skill directory already exists or its marker directory does.
SKILL_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("claude", "~/.claude/skills/defold", "~/.claude"),
    ("opencode", "~/.config/opencode/skills/defold", "~/.config/opencode"),
    ("codex", "~/.codex/skills/defold", "~/.codex"),
    ("agents", "{cwd}/.agents/skills/defold", "{cwd}/.agents"),
    ("github", "{cwd}/.github/skills/defold", "{cwd}/.github"),
)

#: Documented fallback when no harness is detected.
DEFAULT_SKILL_TARGET = Path("~/.config/opencode/skills/defold")


def bundled_skill_dir() -> Path | None:
    """Return the skill directory shipped inside the package, if present."""
    try:
        root = resources.files(PACKAGE)
    except (ModuleNotFoundError, TypeError):
        return None
    candidate = Path(str(root.joinpath("skill", SKILL_DIR_NAME)))
    if (candidate / "SKILL.md").is_file():
        return candidate
    return None


def repository_skill_dir() -> Path:
    """Return the canonical skill directory beside the checkout's ``src``."""
    return Path(__file__).resolve().parents[2] / "skill" / SKILL_DIR_NAME


def skill_source() -> Path:
    """Locate the directory holding ``SKILL.md``.

    Prefers the bundled copy (installed runs) and falls back to the repository
    tree (development runs). Raises ``RuntimeError`` naming both candidates
    when neither carries a ``SKILL.md``.
    """
    bundled = bundled_skill_dir()
    if bundled is not None:
        return bundled
    repository = repository_skill_dir()
    if (repository / "SKILL.md").is_file():
        return repository
    raise RuntimeError(
        "cannot find the defold skill: neither the bundled copy "
        f"({PACKAGE}/skill/{SKILL_DIR_NAME}) nor the repository tree "
        f"({repository}) contains SKILL.md"
    )


def install_skill(target: Path) -> Path:
    """Copy the skill tree into ``target`` and return the installed SKILL.md."""
    source = skill_source()
    destination = Path(os.path.expanduser(str(target)))
    if destination.exists() and not destination.is_dir():
        raise RuntimeError(f"skill target {destination} exists and is not a directory")
    destination.mkdir(parents=True, exist_ok=True)

    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        copied = destination / path.relative_to(source)
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, copied)

    installed = destination / "SKILL.md"
    if not installed.is_file():
        raise RuntimeError(f"skill install into {destination} produced no SKILL.md")
    return installed


def detected_targets() -> list[tuple[str, Path]]:
    """Return every detected harness and the skill directory to install into."""
    detected: list[tuple[str, Path]] = []
    for label, install_template, marker_template in SKILL_TARGETS:
        install_path = resolve_template(install_template)
        marker = resolve_template(marker_template)
        if install_path.is_dir() or marker.is_dir():
            detected.append((label, install_path))
    return detected


def installed_file_count(skill_md: Path) -> int:
    """Count the files below an installed ``SKILL.md``'s directory."""
    return sum(1 for path in skill_md.parent.rglob("*") if path.is_file())
