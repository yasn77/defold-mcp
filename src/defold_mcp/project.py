"""Line-preserving editor for Defold ``game.project`` files.

``game.project`` is INI-style but incompatible with ``configparser``:
keys such as ``dependencies#0`` contain ``#``, sections are case- and
order-significant, and comments and blank lines must survive edits.
This module therefore keeps the file as an ordered list of lines and
indexes them on demand.
"""

import os
import re
from pathlib import Path

from defold_mcp.config import COMPILED_SETTING_KEYS

_SECTION_RE = re.compile(r"^\[(.+)\]\s*$")
_DEPENDENCY_INDEX_RE = re.compile(r"^dependencies#(\d+)$")


def resolve_project_root(start: Path | str | None = None) -> Path:
    """Locate the Defold project root by finding ``game.project``.

    Honours ``DEFOLD_PROJECT_ROOT`` first (the variable names the root
    itself, which must contain ``game.project``); otherwise walks
    upward from ``start`` (or the current directory) through at most
    eight parents.
    """
    env_root = os.environ.get("DEFOLD_PROJECT_ROOT")
    if env_root:
        root = Path(env_root)
        if not (root / "game.project").is_file():
            raise FileNotFoundError(
                f"DEFOLD_PROJECT_ROOT points at {root}, which has no game.project"
            )
        return root

    base = Path(start) if start is not None else Path.cwd()
    for candidate in (base, *base.parents[:8]):
        if (candidate / "game.project").is_file():
            return candidate
    raise FileNotFoundError(
        f"No game.project found at {base} or within its 8 parent directories"
    )


def parse_include_dirs(raw: str) -> list[str]:
    """Split a ``[library] include_dirs`` value on whitespace and/or commas."""
    return [part for part in re.split(r"[\s,]+", raw.strip()) if part]


class GameProject:
    """A line-preserving editor for a single ``game.project`` file."""

    def __init__(self, path: Path | str):
        self._path = Path(path)
        # newline="" disables universal-newline conversion so CRLF files
        # are loaded byte-faithfully.
        with self._path.open(encoding="utf-8", newline="") as handle:
            text = handle.read()
        self._trailing_newline = text.endswith("\n")
        # splitlines() consumes \r, so record the file's newline convention
        # to join with it on output (CRLF-authored files stay CRLF).
        self._newline = "\r\n" if "\r\n" in text else "\n"
        self._lines = text.splitlines()

    @classmethod
    def load(cls, project_root: Path | str) -> "GameProject":
        """Load the ``game.project`` file at the given project root."""
        return cls(Path(project_root) / "game.project")

    # -- indexing ----------------------------------------------------------

    def _index(self):
        """Return (sections, entries): section headers in file order and a
        mapping of (section, key) to line index."""
        sections: list[tuple[str, int]] = []
        entries: dict[tuple[str, str], int] = {}
        current = None
        for i, line in enumerate(self._lines):
            header = _SECTION_RE.match(line)
            if header:
                current = header.group(1).strip()
                sections.append((current, i))
                continue
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                continue
            key = line.partition("=")[0].strip()
            if current is not None:
                entries[(current, key)] = i
        return sections, entries

    def _value_at(self, line_index: int) -> str:
        return self._lines[line_index].partition("=")[2].strip()

    # -- accessors ---------------------------------------------------------

    def get(self, section: str, key: str, default=None) -> str | None:
        """Return the value of ``key`` in ``section``, or ``default``."""
        _, entries = self._index()
        line_index = entries.get((section, key))
        return self._value_at(line_index) if line_index is not None else default

    def sections(self) -> list[str]:
        """Return section names in file order."""
        return [name for name, _ in self._index()[0]]

    def get_dependencies(self, section: str = "project") -> list[str]:
        """Return dependency URLs, indexed ``dependencies#N`` keys ordered
        numerically (``#2`` before ``#10``); falls back to the legacy
        comma-separated ``dependencies`` key when no indexed keys exist."""
        _, entries = self._index()
        indexed = []
        for (sec, key), line_index in entries.items():
            if sec != section:
                continue
            match = _DEPENDENCY_INDEX_RE.match(key)
            if match:
                indexed.append((int(match.group(1)), self._value_at(line_index)))
        if indexed:
            return [value for _, value in sorted(indexed)]
        legacy = self.get(section, "dependencies")
        if legacy is None:
            return []
        return [part.strip() for part in legacy.split(",") if part.strip()]

    def get_include_dirs(self) -> list[str]:
        """Return ``[library] include_dirs`` entries; empty if absent."""
        raw = self.get("library", "include_dirs")
        return parse_include_dirs(raw) if raw is not None else []

    # -- mutations ---------------------------------------------------------

    def set(self, section: str, key: str, value: str) -> None:
        """Set ``key`` in ``section`` in place, appending to the section
        (or creating it at the end of the file) when the key is new.
        Compiled-path keys are auto-suffixed with ``c``."""
        if any(ch in f"{section}{key}{value}" for ch in "\n\r"):
            raise ValueError("section, key and value must be single-line strings")
        if "]" in section:
            raise ValueError("section names must not contain ']'")
        if f"{section}.{key}" in COMPILED_SETTING_KEYS and not value.endswith("c"):
            value = f"{value}c"
        lines = self._lines
        sections, entries = self._index()
        line_index = entries.get((section, key))
        if line_index is not None:
            lines[line_index] = f"{key} = {value}"
            return
        if any(name == section for name, _ in sections):
            header_index = next(i for name, i in sections if name == section)
            later = [i for _, i in sections if i > header_index]
            insert_at = min(later) if later else len(lines)
            # Land after the last member, before any trailing blank run.
            while insert_at > header_index + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, f"{key} = {value}")
            return
        lines.extend(["", f"[{section}]", f"{key} = {value}"])

    def delete(self, section: str, key: str) -> None:
        """Delete ``key`` from ``section`` (no-op when absent)."""
        _, entries = self._index()
        line_index = entries.get((section, key))
        if line_index is not None:
            del self._lines[line_index]

    def delete_section(self, section: str) -> None:
        """Delete a section header and all of its member lines."""
        lines = self._lines
        sections = self._index()[0]
        for name, header_index in sections:
            if name != section:
                continue
            later = [i for _, i in sections if i > header_index]
            end = min(later) if later else len(lines)
            del lines[header_index:end]
            return

    def _rewrite_dependencies(self, urls: list[str]) -> None:
        """Replace every dependency line in ``[project]`` with clean
        sequential ``dependencies#0..#N-1`` keys, leaving all other
        lines (comments, blanks, other sections) untouched."""
        lines = self._lines
        sections, entries = self._index()
        dependency_lines = sorted(
            i
            for (sec, key), i in entries.items()
            if sec == "project"
            and (key == "dependencies" or _DEPENDENCY_INDEX_RE.match(key))
        )
        new_lines = [f"dependencies#{i} = {url}" for i, url in enumerate(urls)]
        if dependency_lines:
            first = dependency_lines[0]
            for i in reversed(dependency_lines[1:]):
                del lines[i]
            lines[first : first + 1] = new_lines
        else:
            if not any(name == "project" for name, _ in sections):
                lines.extend(["", "[project]"])
                lines.extend(new_lines)
                return
            header_index = next(i for name, i in sections if name == "project")
            later = [i for _, i in sections if i > header_index]
            insert_at = min(later) if later else len(lines)
            # Insert after the last member, before any trailing blank run.
            while insert_at > header_index + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines[insert_at:insert_at] = new_lines

    def add_dependency(self, url: str) -> bool:
        """Append a dependency; returns False when already present."""
        if any(ch in url for ch in "\n\r"):
            raise ValueError("url must be a single-line string")
        deps = self.get_dependencies()
        if url in deps:
            return False
        self._rewrite_dependencies(deps + [url])
        return True

    def remove_dependency(self, url: str) -> bool:
        """Remove the first matching dependency; returns False when absent."""
        if any(ch in url for ch in "\n\r"):
            raise ValueError("url must be a single-line string")
        deps = self.get_dependencies()
        if url not in deps:
            return False
        deps.remove(url)
        self._rewrite_dependencies(deps)
        return True

    # -- output ------------------------------------------------------------

    @property
    def text(self) -> str:
        """The current file content as a string."""
        content = self._newline.join(self._lines)
        if self._trailing_newline:
            content += self._newline
        return content

    def save(self, path: Path | str | None = None) -> None:
        """Write the lines back atomically (``.tmp`` + ``os.replace``)."""
        target = Path(path) if path is not None else self._path
        tmp = target.with_name(target.name + ".tmp")
        # newline="" stops os.linesep translation so CRLF content is
        # written byte-faithfully on every platform.
        tmp.write_text(self.text, encoding="utf-8", newline="")
        os.replace(tmp, target)
