"""Tests for the GitHub Actions workflows and the release configuration.

The runner is unavailable here (no Node, so no ``act``), so these tests pin
the files that GitHub will interpret: every workflow must parse, every action
must be SHA-pinned with a version comment, the runner labels must be the
Ubicloud x64 fleet, and the release configuration must agree with the three
places release-please rewrites the version.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

from defold_mcp.config import get_cache_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOW_DIR / "ci.yml"
RELEASE_WORKFLOW = WORKFLOW_DIR / "release.yml"
RELEASE_CONFIG = REPO_ROOT / ".release-please-config.json"
RELEASE_MANIFEST = REPO_ROOT / ".release-please-manifest.json"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
RENOVATE_CONFIG = REPO_ROOT / "renovate.json"

# Every workflow parses, including the pre-existing OpenWiki one. Only the two
# workflows added for the CI/release phase are expected to run on Ubicloud.
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.y*ml"))
NEW_WORKFLOWS = (CI_WORKFLOW, RELEASE_WORKFLOW)

UBICLOUD_RUNNER = re.compile(
    r"^ubicloud-standard-(?:2|4|8|16|30)(?:-ubuntu-2204)?$"
)
SHA_PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"#\s*v\d+(?:\.\d+)*\s*$")
# The live cache key carries the Defold version literally: a version-agnostic
# fallback would restore the previous version's builtins/ref-doc zips.
CACHE_KEY = re.compile(
    r"^defold-toolchain-(?P<version>\d+\.\d+\.\d+)-"
    r"\$\{\{\s*runner\.os\s*\}\}$"
)
LIVE_SOURCE = REPO_ROOT / "tests" / "test_mcp_stdio_e2e.py"
LIVE_SPEC = re.compile(r'"spec":\s*"(?P<version>\d+\.\d+\.\d+)"')


def _load_workflow(path: Path) -> dict:
    """Parse ``path`` and normalise the YAML 1.1 ``on`` trigger key."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), path
    # PyYAML resolves the bare ``on`` key as the boolean ``True``. GitHub
    # reads it as the trigger map, so restore the spelling GitHub expects.
    if True in document:
        document["on"] = document.pop(True)
    return document


def _steps(document: dict) -> list[dict]:
    """Return every step of every job in ``document``."""
    return [
        step
        for job in document["jobs"].values()
        for step in job.get("steps", [])
    ]


def _runs(step: dict) -> str:
    """Return the command a step runs, tolerating multi-line blocks."""
    return str(step.get("run", "")).strip()


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_workflow_parses(workflow: Path) -> None:
    document = _load_workflow(workflow)
    assert document["on"], f"{workflow.name} has no trigger"
    assert document["jobs"], f"{workflow.name} has no jobs"


def test_expected_workflows_exist() -> None:
    # Keeps the parametrised tests above from passing vacuously.
    assert CI_WORKFLOW.is_file()
    assert RELEASE_WORKFLOW.is_file()


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_actions_are_sha_pinned_with_a_version_comment(workflow: Path) -> None:
    text = workflow.read_text(encoding="utf-8")
    entries = re.findall(r"uses:\s*(\S+)(.*)$", text, re.MULTILINE)
    assert entries, f"{workflow.name} uses no actions"
    for uses, trailing in entries:
        if uses.startswith("./"):
            continue
        assert SHA_PINNED.match(uses), f"{workflow.name}: {uses} is not pinned"
        assert VERSION_COMMENT.search(trailing), (
            f"{workflow.name}: {uses} needs a '# vN' comment"
        )


@pytest.mark.parametrize(
    "workflow", NEW_WORKFLOWS, ids=lambda path: path.name
)
def test_runners_are_ubicloud_x64(workflow: Path) -> None:
    document = _load_workflow(workflow)
    labels: list[str] = []
    for job in document["jobs"].values():
        runs_on = job["runs-on"]
        labels.extend([runs_on] if isinstance(runs_on, str) else runs_on)
    assert labels
    for label in labels:
        assert UBICLOUD_RUNNER.match(label), f"{workflow.name}: {label}"
    # The fleet intent is x64: `${{ runner.os }}` in the cache key is
    # arch-blind, so an arm job would restore x64 engine binaries under it.
    assert UBICLOUD_RUNNER.match("ubicloud-standard-2")
    assert not UBICLOUD_RUNNER.match("ubicloud-standard-2-arm")


def test_ci_triggers_and_permissions() -> None:
    document = _load_workflow(CI_WORKFLOW)
    assert "pull_request" in document["on"]
    assert document["on"]["push"]["branches"] == ["master"]
    assert document["permissions"] == {"contents": "read"}


def test_ci_runs_the_three_mise_tasks() -> None:
    document = _load_workflow(CI_WORKFLOW)
    assert {"unit", "handshake", "live"} <= set(document["jobs"])

    commands = "\n".join(_runs(step) for step in _steps(document))
    for task in ("mise run test", "mise run live", "mise run handshake"):
        assert task in commands, f"ci.yml never runs '{task}'"


def test_ci_cancels_superseded_pull_request_runs() -> None:
    concurrency = _load_workflow(CI_WORKFLOW)["concurrency"]
    assert "github.ref" in concurrency["group"]
    assert "pull_request" in str(concurrency["cancel-in-progress"])


def test_live_job_caches_the_defold_toolchain(tmp_path, monkeypatch) -> None:
    document = _load_workflow(CI_WORKFLOW)
    caches = [
        step
        for step in document["jobs"]["live"]["steps"]
        if str(step.get("uses", "")).startswith("actions/cache@")
    ]
    assert len(caches) == 1, "the live job needs exactly one cache step"

    settings = caches[0]["with"]
    assert settings["path"] == "~/.cache/defold-mcp"
    # The path must be the product's documented fallback cache dir, so a
    # future get_cache_dir() change fails the gate rather than silently
    # missing the cache on the runner.
    monkeypatch.delenv("DEFOLD_MCP_CACHE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Path(settings["path"]).expanduser() == get_cache_dir()

    # The key pins the Defold version in two places that must not drift.
    key = CACHE_KEY.match(settings["key"])
    assert key, settings["key"]
    spec = LIVE_SPEC.search(LIVE_SOURCE.read_text(encoding="utf-8"))
    assert spec, "the live smoke no longer pins a toolchain spec"
    assert key["version"] == spec["version"]

    # A fallback would restore the previous version's builtins/ref-doc zips.
    assert "restore-keys" not in settings


def test_every_job_has_a_timeout() -> None:
    document = _load_workflow(CI_WORKFLOW)
    for name, job in document["jobs"].items():
        assert job.get("timeout-minutes"), f"{name} has no timeout"
    assert document["jobs"]["live"]["timeout-minutes"] >= 30


def test_release_workflow_uses_release_please_and_writes() -> None:
    document = _load_workflow(RELEASE_WORKFLOW)
    assert document["on"]["push"]["branches"] == ["master"]

    steps = _steps(document)
    actions = [str(step.get("uses", "")) for step in steps]
    assert any(
        action.startswith("googleapis/release-please-action@")
        for action in actions
    )
    assert document["permissions"]["contents"] == "write"
    assert document["permissions"]["pull-requests"] == "write"


def test_release_config_uses_the_python_strategy() -> None:
    config = json.loads(RELEASE_CONFIG.read_text(encoding="utf-8"))
    package = config["packages"]["."]
    # The python strategy rewrites [project] version and src/<pkg>/__init__.py.
    assert package["release-type"] == "python"
    # Plain vX.Y.Z tags, not defold-mcp-vX.Y.Z.
    assert package["include-component-in-tag"] is False
    assert package["changelog-path"] == "CHANGELOG.md"


def test_release_manifest_seeds_the_current_version() -> None:
    manifest = json.loads(RELEASE_MANIFEST.read_text(encoding="utf-8"))
    assert manifest == {".": "0.1.0"}


def test_version_sources_agree() -> None:
    """release-please bumps all three; they must start in step."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    version = pyproject["project"]["version"]

    manifest = json.loads(RELEASE_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["."] == version

    init = (REPO_ROOT / "src" / "defold_mcp" / "__init__.py").read_text("utf-8")
    assert f'__version__ = "{version}"' in init


def test_changelog_is_seeded_for_release_please() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    assert text.startswith("# Changelog\n")
    assert "## [Unreleased]" in text


def _renovate() -> dict:
    return json.loads(RENOVATE_CONFIG.read_text(encoding="utf-8"))


def test_renovate_config_parses_and_automerges_pr() -> None:
    config = _renovate()
    # Renovate's own automerge, not GitHub's: native auto-merge needs
    # branch-protection required checks, which this repository does not rely
    # on. Renovate merges only once the PR's own checks pass.
    assert config["automerge"] is True
    assert config["automergeType"] == "pr"
    assert config["automergeStrategy"] == "squash"
    assert config["platformAutomerge"] is False

    assert config["rebaseWhen"] == "behind-base-branch"
    assert config["schedule"] == ["at any time"]
    assert config["prHourlyLimit"] == 2
    assert config["prConcurrentLimit"] == 3
    # The schema types dependencyDashboard as an object; a bare boolean would
    # leave ``enabled`` unset and silently keep the dashboard off.
    assert config["dependencyDashboard"] == {"enabled": True, "autoclose": True}
    # No lockfile is tracked (uv.lock is ignored), so there is nothing to
    # maintain and the option must stay out.
    assert "lockFileMaintenance" not in config


def test_renovate_groups_are_logical() -> None:
    config = _renovate()
    rules = config["packageRules"]
    groups = {rule["groupName"]: rule for rule in rules if "groupName" in rule}
    assert {
        "Python dependencies",
        "GitHub Actions",
        "mise toolchain",
        "Container base images",
    } <= set(groups)

    assert groups["Python dependencies"]["matchCategories"] == ["python"]
    assert groups["Python dependencies"]["matchUpdateTypes"] == ["minor", "patch"]

    actions = groups["GitHub Actions"]
    assert actions["matchManagers"] == ["github-actions"]
    assert actions["matchUpdateTypes"] == ["minor", "patch"]
    # Every action here is SHA-pinned; keep the digests refreshed.
    assert actions["pinDigests"] is True

    assert groups["mise toolchain"]["matchManagers"] == ["mise"]

    container = groups["Container base images"]
    assert container["matchManagers"] == ["dockerfile"]
    assert container["matchUpdateTypes"] == ["minor", "patch", "digest"]

    major = [rule for rule in rules if rule.get("matchUpdateTypes") == ["major"]]
    assert len(major) == 1, "exactly one major-update rule"
    assert major[0]["automerge"] is False
    assert "major-update" in major[0]["addLabels"]


def test_renovate_vulnerability_alerts_not_automerged() -> None:
    alerts = _renovate()["vulnerabilityAlerts"]
    assert alerts["enabled"] is True
    assert alerts["automerge"] is False
    assert alerts["labels"] == ["security"]


def test_renovate_schema_and_extends() -> None:
    config = _renovate()
    assert (
        config["$schema"]
        == "https://docs.renovatebot.com/renovate-schema.json"
    )
    assert "config:recommended" in config["extends"]
    assert ":semanticCommits" in config["extends"]
    assert ":semanticCommitTypeAll(chore)" in config["extends"]
    # The automerge rationale is carried in ``description``: renovate.json is
    # JSON here (the tests parse it with ``json.loads``), so no comments.
    assert config["description"]
    assert config["labels"] == ["renovate", "dependencies"]

