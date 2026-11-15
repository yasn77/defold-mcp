"""Tests for the packaging, skill-install and integration-install CLI helpers.

Everything here is hermetic: harness configuration is written under a
monkeypatched ``HOME`` (or a temporary working directory), never into the real
``~/.config`` or ``~/.claude``. The three behaviours that carry the most risk
are pinned explicitly: the wheel must force-include the skill, the installer
must copy (not symlink) the tree, and a registration must be idempotent and
round-trip its own serialisation before the file is replaced.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
import yaml

from defold_mcp import __main__ as main_module
from defold_mcp import integrations
from defold_mcp import skill as skill_module

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_SKILL = REPO_ROOT / "skill" / "defold"
SKILL_REFERENCES = (
    "architecture-and-lua-rules.md",
    "cross-platform-and-genres.md",
    "proto-schemas-quickref.md",
    "shaders-and-materials.md",
)


class _PathTraversable:
    """Minimal ``importlib.resources`` Traversable backed by a real path."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def joinpath(self, *parts: str) -> _PathTraversable:
        return _PathTraversable(self._path.joinpath(*parts))

    def __str__(self) -> str:
        return str(self._path)


class _FakeResources:
    """Stand-in for the ``importlib.resources`` module in ``skill.py``."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def files(self, package: str) -> _PathTraversable:
        assert package == "defold_mcp"
        return _PathTraversable(self._root)


def _fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return home


def test_skill_source_bundled_and_repo_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No bundled copy (the editable development install): fall back to the repo.
    monkeypatch.setattr(skill_module, "resources", _FakeResources(tmp_path / "absent"))
    assert skill_module.skill_source() == REPO_SKILL

    # A bundled copy wins when it carries a SKILL.md (the installed wheel).
    bundled = tmp_path / "site-packages" / "defold_mcp"
    (bundled / "skill" / "defold").mkdir(parents=True)
    (bundled / "skill" / "defold" / "SKILL.md").write_text("# bundled\n", encoding="utf-8")
    monkeypatch.setattr(skill_module, "resources", _FakeResources(bundled))
    assert skill_module.skill_source() == bundled / "skill" / "defold"


def test_skill_source_raises_when_neither_candidate_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(skill_module, "resources", _FakeResources(tmp_path / "absent"))
    monkeypatch.setattr(skill_module, "repository_skill_dir", lambda: tmp_path / "no-repo")
    with pytest.raises(RuntimeError, match="SKILL.md"):
        skill_module.skill_source()


def test_install_skill_copies_tree(tmp_path: Path) -> None:
    target = tmp_path / "installed"
    installed = skill_module.install_skill(target)

    assert installed == target / "SKILL.md"
    assert installed.read_bytes() == (REPO_SKILL / "SKILL.md").read_bytes()
    for reference in SKILL_REFERENCES:
        copied = target / "references" / reference
        assert copied.is_file()
        assert copied.read_bytes() == (REPO_SKILL / "references" / reference).read_bytes()
    # Copy, never a symlink: the bundled source lives in site-packages.
    assert not installed.is_symlink()


def test_skill_install_target_override_and_autodetect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    explicit = tmp_path / "explicit"
    assert main_module.main(["skill", "install", "--target", str(explicit)]) == 0
    assert (explicit / "SKILL.md").is_file()
    assert str(explicit) in capsys.readouterr().out

    home = _fake_home(tmp_path)
    (home / ".claude").mkdir(parents=True)
    (home / ".config" / "opencode").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)

    assert main_module.main(["skill", "install"]) == 0
    output = capsys.readouterr().out
    claude_skill = home / ".claude" / "skills" / "defold"
    opencode_skill = home / ".config" / "opencode" / "skills" / "defold"
    assert (claude_skill / "SKILL.md").is_file()
    assert (opencode_skill / "SKILL.md").is_file()
    assert "claude" in output
    assert "opencode" in output
    # Undetected harnesses are left alone.
    assert not (home / ".codex").exists()
    assert not (project / ".agents").exists()


def test_integration_register_opencode_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _fake_home(tmp_path)
    config = home / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {"$schema": "https://opencode.ai/config.json", "mcp": {"other": {"type": "local", "command": ["other"]}}},
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    result = integrations.register(
        "opencode", command=["/opt/bin/defold-mcp"], env={"DEFOLD_MCP_CACHE_DIR": "/tmp/cache"}
    )

    assert result["installed"] is True
    assert result["backup"] == f"{config}.bak-defold-mcp"
    assert Path(result["backup"]).is_file()
    document = json.loads(config.read_text(encoding="utf-8"))
    entry = document["mcp"]["defold"]
    assert entry["type"] == "local"
    assert entry["command"] == ["/opt/bin/defold-mcp"]
    assert entry["environment"] == {"DEFOLD_MCP_CACHE_DIR": "/tmp/cache"}
    assert entry["timeout"] == 900_000
    assert document["mcp"]["other"] == {"type": "local", "command": ["other"]}
    assert document["$schema"] == "https://opencode.ai/config.json"

    before = config.read_bytes()
    again = integrations.register(
        "opencode", command=["/opt/bin/defold-mcp"], env={"DEFOLD_MCP_CACHE_DIR": "/tmp/cache"}
    )
    assert again["installed"] is False
    assert config.read_bytes() == before


def test_integration_register_codex_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _fake_home(tmp_path)
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "other"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    result = integrations.register(
        "codex", command=["/opt/bin/defold-mcp"], env={"DEFOLD_MCP_CACHE_DIR": "/tmp/cache"}
    )

    assert result["installed"] is True
    document = tomllib.loads(config.read_text(encoding="utf-8"))
    entry = document["mcp_servers"]["defold"]
    assert entry["command"] == "/opt/bin/defold-mcp"
    assert entry["args"] == []
    assert entry["env"] == {"DEFOLD_MCP_CACHE_DIR": "/tmp/cache"}
    assert entry["startup_timeout_sec"] == 30
    assert entry["tool_timeout_sec"] == 900
    assert document["mcp_servers"]["other"]["command"] == "other"
    assert document["model"] == "gpt-5"


def test_integration_register_vscode_json_and_zed_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _fake_home(tmp_path)
    project = tmp_path / "project"
    (project / ".vscode").mkdir(parents=True)
    vscode = project / ".vscode" / "mcp.json"
    vscode.write_text(json.dumps({"servers": {"other": {"type": "stdio", "command": "other"}}}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)

    result = integrations.register("vscode", command=["/opt/bin/defold-mcp", "--log-level", "DEBUG"], env={})
    assert result["installed"] is True
    servers = json.loads(vscode.read_text(encoding="utf-8"))["servers"]
    assert servers["defold"] == {
        "type": "stdio",
        "command": "/opt/bin/defold-mcp",
        "args": ["--log-level", "DEBUG"],
        "env": {},
    }
    assert servers["other"] == {"type": "stdio", "command": "other"}

    zed = home / ".config" / "zed" / "settings.json"
    zed.parent.mkdir(parents=True)
    zed.write_text("{}", encoding="utf-8")
    zed_result = integrations.register("zed", command=["/opt/bin/defold-mcp"], env={"X": "1"})
    assert zed_result["installed"] is True
    context = json.loads(zed.read_text(encoding="utf-8"))["context_servers"]
    assert context["defold"] == {"command": {"path": "/opt/bin/defold-mcp", "args": [], "env": {"X": "1"}}}

    # The YAML writer is part of the registry contract; round-trip it directly.
    yaml_path = tmp_path / "settings.yaml"
    yaml_path.write_text("context_servers:\n  old:\n    command:\n      path: /bin/old\n", encoding="utf-8")
    document = integrations.read_config(yaml_path, "yaml")
    assert document["context_servers"]["old"]["command"]["path"] == "/bin/old"
    document["context_servers"]["defold"] = {"command": {"path": "/bin/new"}}
    integrations.write_config(yaml_path, document, "yaml")
    assert yaml.safe_load(yaml_path.read_text(encoding="utf-8"))["context_servers"]["defold"]["command"]["path"] == "/bin/new"


def test_integration_register_absent_config_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _fake_home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)

    result = integrations.register("windsurf", command=["/opt/bin/defold-mcp"], env={})

    assert result["installed"] is False
    assert "no config file" in result["error"]
    assert not (home / ".codeium").exists()


def test_integration_register_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _fake_home(tmp_path)
    config = home / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"mcp": {"other": {"type": "local", "command": ["other"], "environment": {"SECRET": "value"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    before = config.read_bytes()
    result = integrations.register(
        "opencode", command=["/opt/bin/defold-mcp"], env={}, dry_run=True
    )

    assert result["dry_run"] is True
    assert result["installed"] is False
    assert config.read_bytes() == before
    assert '"defold"' in result["preview"]
    # The preview carries the edit, not the rest of the document: unrelated
    # entries may hold credentials that must not reach a log.
    assert "SECRET" not in result["preview"]
    assert not Path(f"{config}.bak-defold-mcp").exists()


def test_cli_subcommand_parsing() -> None:
    parser = main_module._build_parser()

    skill_args = parser.parse_args(["skill", "install", "--target", "/tmp/x"])
    assert skill_args.command == "skill"
    assert skill_args.skill_command == "install"
    assert skill_args.target == Path("/tmp/x")

    integration_args = parser.parse_args(["integration", "install", "opencode", "--dry-run"])
    assert integration_args.command == "integration"
    assert integration_args.integration_command == "install"
    assert integration_args.harness == "opencode"
    assert integration_args.dry_run is True

    # --command is one shell string: argparse would treat "-m" as an option
    # flag under nargs="+", making the module form inexpressible.
    override_args = parser.parse_args(
        ["integration", "install", "codex", "--command", "python -m defold_mcp"]
    )
    assert override_args.command_override == "python -m defold_mcp"

    serve_args = parser.parse_args([])
    assert serve_args.command is None
    assert serve_args.transport == "stdio"
    assert serve_args.host == "127.0.0.1"
    assert serve_args.port == 9810
    assert serve_args.log_level == "INFO"

    http_args = parser.parse_args(["--transport", "streamable-http", "--port", "1234"])
    assert http_args.command is None
    assert http_args.transport == "streamable-http"
    assert http_args.port == 1234

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["integration", "install", "nope"])
    assert exit_info.value.code != 0


def test_toml_emitter_handles_hostile_scalars() -> None:
    # The emitter hand-rolls TOML; every value here has a documented hazard:
    # "#" (comment), quotes, a newline, a dotted key and a non-BMP scalar.
    value = '# not a comment "quoted"\nsecond line 😀 dolor sit'
    document = {
        "outer": {
            "dotted.key": value,
            "with#hash": ["a#b", '"quoted"', "😀"],
            "plain": 7,
        }
    }

    text = integrations._dump_toml(document)

    assert tomllib.loads(text) == document
    # Raw non-BMP text: ensure_ascii=True would emit a surrogate pair, which
    # TOML forbids.
    assert "😀" in text
    assert "\\ud83d" not in text.lower()


def test_integration_backup_preserved_on_second_different_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _fake_home(tmp_path)
    config = home / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    original = json.dumps({"mcp": {}}, indent=2).encode("utf-8")
    config.write_bytes(original)
    monkeypatch.setenv("HOME", str(home))

    first = integrations.register("opencode", command=["/opt/bin/defold-mcp"], env={"A": "1"})
    assert first["installed"] is True
    assert first["replaced"] is False
    backup = Path(first["backup"])
    assert backup.read_bytes() == original

    second = integrations.register("opencode", command=["/usr/local/bin/defold-mcp"], env={"A": "2"})
    assert second["installed"] is True
    assert second["replaced"] is True
    assert second["backup"] is None
    # The backup keeps the pre-first-edit state, not the replaced entry.
    assert backup.read_bytes() == original
    entry = json.loads(config.read_text(encoding="utf-8"))["mcp"]["defold"]
    assert entry["command"] == ["/usr/local/bin/defold-mcp"]


def test_integration_install_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _fake_home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)

    # A named harness with no config file is a failure.
    assert main_module.main(["integration", "install", "windsurf"]) == 1
    assert "no config file" in capsys.readouterr().out

    # `all` treats absent configs as skips, not errors.
    assert main_module.main(["integration", "install", "all"]) == 0


def test_wheel_force_include_configured() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)

    wheel = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["force-include"]["skill/defold"] == "defold_mcp/skill/defold"
