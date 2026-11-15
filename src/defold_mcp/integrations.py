"""Registration of the ``defold`` MCP server in supported harness configs.

Every shape here mirrors ``docs/INSTALL.md``. Registration is deliberately
conservative:

* a missing config file is reported and skipped, never created;
* the previous file is copied to ``<config>.bak-defold-mcp`` before the first
  edit, and only when no such backup exists;
* a document that already carries the identical entry is left alone;
* the merged document is re-parsed with the same parser that will read it
  before the file is replaced, and the replacement is atomic.

JSON and TOML are handled by the standard library; YAML needs the optional
``PyYAML`` development dependency and is imported lazily so the server keeps
its three-package runtime footprint.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FORMATS = ("json", "toml", "yaml")
BACKUP_SUFFIX = ".bak-defold-mcp"


@dataclass(frozen=True)
class Harness:
    """One harness: where its config lives and which entry table it reads."""

    name: str
    label: str
    path_template: str
    fmt: str
    table_key: str

    def config_path(self) -> Path:
        return resolve_template(self.path_template)


def resolve_template(template: str) -> Path:
    """Expand ``~`` and the ``{cwd}`` placeholder in a path template."""
    return Path(os.path.expanduser(template.replace("{cwd}", str(Path.cwd()))))


# The order is the order `--harness all` reports in.
HARNESSES: dict[str, Harness] = {
    "claude": Harness("claude", "Claude Code", "{cwd}/.mcp.json", "json", "mcpServers"),
    "codex": Harness("codex", "OpenAI Codex CLI", "~/.codex/config.toml", "toml", "mcp_servers"),
    "cursor": Harness("cursor", "Cursor", "{cwd}/.cursor/mcp.json", "json", "mcpServers"),
    "vscode": Harness("vscode", "VS Code (Copilot agent mode)", "{cwd}/.vscode/mcp.json", "json", "servers"),
    "gemini": Harness("gemini", "Gemini CLI", "~/.gemini/settings.json", "json", "mcpServers"),
    "opencode": Harness("opencode", "OpenCode", "~/.config/opencode/opencode.json", "json", "mcp"),
    "windsurf": Harness("windsurf", "Windsurf", "~/.codeium/windsurf/mcp_config.json", "json", "mcpServers"),
    "zed": Harness("zed", "Zed", "~/.config/zed/settings.json", "json", "context_servers"),
}


def resolved_command(explicit: list[str] | None = None) -> list[str]:
    """Return the server invocation to register.

    An explicit ``--command`` wins; otherwise the console script on ``PATH``;
    otherwise the current interpreter with ``-m defold_mcp``, which works for a
    ``uvx``/zipapp-style environment where no script was installed.
    """
    if explicit:
        return list(explicit)
    executable = shutil.which("defold-mcp")
    if executable:
        return [executable]
    return [sys.executable, "-m", "defold_mcp"]


def default_env() -> dict[str, str]:
    """Return the environment block every registration carries."""
    return {"DEFOLD_MCP_CACHE_DIR": str(Path.home() / ".cache" / "defold-mcp")}


def build_entry(harness: Harness, command: list[str], env: dict[str, str]) -> dict[str, Any]:
    """Build the harness-specific ``defold`` entry from one invocation."""
    if harness.name == "opencode":
        return {
            "type": "local",
            "command": list(command),
            "environment": dict(env),
            "timeout": 900_000,
            "enabled": True,
        }
    if harness.name == "zed":
        return {"command": {"path": command[0], "args": list(command[1:]), "env": dict(env)}}
    if harness.name == "vscode":
        return {"type": "stdio", "command": command[0], "args": list(command[1:]), "env": dict(env)}

    entry: dict[str, Any] = {"command": command[0], "args": list(command[1:]), "env": dict(env)}
    if harness.name == "claude":
        entry["timeout"] = 900_000
    if harness.name == "codex":
        entry["startup_timeout_sec"] = 30
        entry["tool_timeout_sec"] = 900
    return entry


def read_config(path: Path, fmt: str) -> dict[str, Any]:
    """Parse a harness config file into a mapping (empty file -> ``{}``)."""
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    if fmt == "json":
        document = json.loads(text)
    elif fmt == "toml":
        document = tomllib.loads(text)
    elif fmt == "yaml":
        document = _yaml().safe_load(text) or {}
    else:
        raise ValueError(f"unsupported config format: {fmt}")
    if not isinstance(document, dict):
        raise ValueError(f"{path} does not contain a mapping at the top level")
    return document


def write_config(path: Path, document: dict[str, Any], fmt: str) -> None:
    """Serialise, round-trip validate and atomically replace ``path``."""
    text = serialise_config(document, fmt)
    _validate_round_trip(text, fmt)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".defold-mcp.tmp")
    temporary.write_text(text, encoding="utf-8")
    if path.exists():
        shutil.copymode(path, temporary)
    os.replace(temporary, path)


def serialise_config(document: dict[str, Any], fmt: str) -> str:
    """Render a document in the harness's own format."""
    if fmt == "json":
        return json.dumps(document, indent=2) + "\n"
    if fmt == "toml":
        return _dump_toml(document)
    if fmt == "yaml":
        return _yaml().safe_dump(document, sort_keys=False)
    raise ValueError(f"unsupported config format: {fmt}")


def register(
    harness: str,
    *,
    command: list[str],
    env: dict[str, str],
    name: str = "defold",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Merge the server entry into a harness config and report the result.

    Returns ``{harness, path, installed, replaced, backup}`` plus ``error``
    when the config is absent or unreadable, and ``dry_run``/``preview`` when
    nothing was written. ``replaced`` marks a write over an existing entry
    with different values, so the caller can report it distinctly.
    """
    spec = HARNESSES[harness]
    path = spec.config_path()
    result: dict[str, Any] = {
        "harness": harness,
        "label": spec.label,
        "path": str(path),
        "installed": False,
        "replaced": False,
        "backup": None,
    }

    if not path.is_file():
        result["error"] = f"no config file at {path}"
        return result

    read_errors: tuple[type[BaseException], ...] = (ValueError, OSError)
    if spec.fmt == "yaml":
        read_errors += (_yaml().YAMLError,)
    try:
        document = read_config(path, spec.fmt)
    except read_errors as exc:
        result["error"] = f"cannot read {path}: {exc}"
        return result

    table = document.setdefault(spec.table_key, {})
    if not isinstance(table, dict):
        result["error"] = f"{path} has a non-mapping {spec.table_key!r} entry"
        return result

    entry = build_entry(spec, command, env)
    if table.get(name) == entry:
        return result

    replaced = name in table
    result["replaced"] = replaced
    table[name] = entry
    if dry_run:
        result["dry_run"] = True
        # Preview only the edit: the surrounding document may hold unrelated
        # credentials that have no business in a terminal or CI log.
        result["preview"] = serialise_config({spec.table_key: {name: entry}}, spec.fmt)
        return result

    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(path, backup)
        result["backup"] = str(backup)

    try:
        write_config(path, document, spec.fmt)
    except (OSError, TypeError, ValueError) as exc:
        result["error"] = f"cannot write {path}: {exc}"
        return result

    result["installed"] = True
    return result


def _yaml() -> Any:
    """Import PyYAML lazily: it is a development extra, not a runtime need."""
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - dev extra is present in the gate
        raise RuntimeError("PyYAML is required for YAML harness configs; install the dev extra") from exc
    return yaml


def _validate_round_trip(text: str, fmt: str) -> None:
    """Re-parse serialised text and require a mapping back."""
    if fmt == "json":
        document = json.loads(text)
    elif fmt == "toml":
        document = tomllib.loads(text)
    elif fmt == "yaml":
        document = _yaml().safe_load(text)
    else:
        raise ValueError(f"unsupported config format: {fmt}")
    if not isinstance(document, dict):
        raise ValueError(f"serialised {fmt} document did not round-trip to a mapping")


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _toml_key(key: str) -> str:
    # Quoted keys use the same escape mechanism as values, so they take the
    # same ensure_ascii=False treatment.
    return key if _BARE_KEY.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        # JSON string escapes are a strict subset of TOML basic-string
        # escapes, with one exception: ensure_ascii=True emits surrogate pairs
        # for non-BMP scalars, which TOML rejects. Raw non-ASCII is legal in a
        # TOML basic string; control characters stay \u-escaped either way.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"cannot serialise {type(value).__name__} to TOML")


def _dump_toml(document: dict[str, Any]) -> str:
    """Serialise the subset of TOML a harness config uses.

    ``tomllib`` reads TOML but cannot write it, and the project deliberately
    carries no third-party TOML writer. Harness configs are shallow: scalars,
    arrays and nested tables, which this emitter covers. Comments are lost;
    the round-trip validation catches anything the emitter gets wrong.
    """
    lines: list[str] = []

    def emit(table: dict[str, Any], prefix: tuple[str, ...]) -> None:
        if prefix:
            lines.append("")
            lines.append("[" + ".".join(_toml_key(part) for part in prefix) + "]")
        for key, value in table.items():
            if isinstance(value, dict):
                continue
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        for key, value in table.items():
            if isinstance(value, dict):
                emit(value, prefix + (key,))

    emit(document, ())
    return "\n".join(lines).lstrip("\n") + "\n"
