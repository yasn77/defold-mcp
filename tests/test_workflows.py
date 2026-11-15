"""Tests for the CI/release configuration, the container image and the docs.

The runner is unavailable here (no Node, so no ``act``), and no container
runtime is available either, so these tests pin the files that CI and the
container build will interpret: every workflow must parse, every action must be
SHA-pinned with a version comment, the runner labels must be the Ubicloud x64
fleet, the release configuration must agree with the three places
release-please rewrites the version, the ``Containerfile`` must stay a
two-stage Chainguard build, the container build must go through buildx, a
release must publish the image to GHCR from its own run, and the committed
documentation must carry no local paths.
"""

from __future__ import annotations

import fnmatch
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
RELEASE_IMAGE_WORKFLOW = WORKFLOW_DIR / "release-image.yml"
OPENWIKI_WORKFLOW = WORKFLOW_DIR / "openwiki-update.yml"
RELEASE_CONFIG = REPO_ROOT / ".release-please-config.json"
RELEASE_MANIFEST = REPO_ROOT / ".release-please-manifest.json"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
RENOVATE_CONFIG = REPO_ROOT / "renovate.json"
CONTAINERFILE = REPO_ROOT / "Containerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"

# Every workflow parses. The repository keeps the CI gate and the release, both
# on the Ubicloud fleet; the image is published by a job inside the release.
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


def _dockerignore_patterns() -> list[str]:
    """Return the active ``.dockerignore`` patterns, comments removed.

    Each pattern is normalised the way Docker reads it: surrounding
    whitespace, a leading ``./`` and a trailing ``/`` carry no meaning.
    """
    patterns: list[str] = []
    for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        if pattern.startswith("./"):
            pattern = pattern[2:]
        patterns.append(pattern.rstrip("/"))
    return patterns


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_workflow_parses(workflow: Path) -> None:
    document = _load_workflow(workflow)
    assert document["on"], f"{workflow.name} has no trigger"
    assert document["jobs"], f"{workflow.name} has no jobs"


def test_expected_workflows_exist() -> None:
    # Keeps the parametrised tests above from passing vacuously.
    assert CI_WORKFLOW.is_file()
    assert RELEASE_WORKFLOW.is_file()


def test_openwiki_update_workflow_removed() -> None:
    """The wiki refresh is manual: no scheduled workflow may creep back in.

    The user maintains ``openwiki/`` by hand through the OpenWiki lifecycle and
    asked for the scheduled CI refresh to go, so a reintroduced file — or a
    stray reference to one from another workflow — fails here rather than
    resurrecting a job nobody runs.
    """
    assert not OPENWIKI_WORKFLOW.exists()
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in WORKFLOWS
    ).lower()
    assert "openwiki" not in text
    assert "npm install --global openwiki" not in text


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
    # Writing is granted per job, so the release PR machinery carries it and
    # the image job does not inherit it.
    permissions = document["jobs"]["release-please"]["permissions"]
    assert permissions["contents"] == "write"
    assert permissions["pull-requests"] == "write"
    assert document["permissions"] == {"contents": "read"}


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
    # Digest updates are grouped too: every action here is SHA-pinned, so the
    # SHA refreshes must arrive as one PR rather than one per action.
    assert actions["matchUpdateTypes"] == ["minor", "patch", "digest"]
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


def test_containerfile_exists_and_uses_chainguard() -> None:
    """The image is a two-stage build on the Chainguard Python images."""
    text = CONTAINERFILE.read_text(encoding="utf-8")

    assert "FROM cgr.dev/chainguard/python:latest-dev AS build" in text
    assert "FROM cgr.dev/chainguard/python:latest\n" in text

    # The venv is built in the dev variant and copied into the minimal one.
    assert re.search(
        r"^COPY --chown=nonroot:nonroot --from=build /app/venv /app/venv$",
        text,
        re.MULTILINE,
    )
    assert 'ENV PATH="/app/venv/bin:$PATH"' in text
    assert 'ENTRYPOINT ["defold-mcp"]' in text
    assert "USER nonroot" in text

    # The cache directory is seeded in the image so a named volume mounted over
    # it inherits `nonroot` ownership rather than a root-owned mountpoint the
    # server cannot write to.
    assert re.search(
        r"^COPY --chown=nonroot:nonroot --from=build /app/cache-seed/ "
        r"/home/nonroot/\.cache/defold-mcp/$",
        text,
        re.MULTILINE,
    )

    # The runtime image ships no shell, so it may hold no RUN instruction.
    runtime = re.split(
        r"^FROM cgr\.dev/chainguard/python:latest\s*$",
        text,
        maxsplit=1,
        flags=re.MULTILINE,
    )[1]
    assert "RUN " not in runtime, "the minimal image has no shell to run"

    ignored = set(_dockerignore_patterns())
    # A stray .venv would copy a host-architecture interpreter into the build.
    assert {".venv", ".git", "__pycache__", ".pytest_cache"} <= ignored


def test_dockerignore_keeps_the_skill_source_in_the_build_context() -> None:
    """The wheel force-includes ``skill/defold``, so the context must carry it.

    ``pyproject.toml`` maps ``skill/defold`` into the package and hatchling
    reads that source from the build context, so excluding ``skill/`` fails the
    wheel build with "Forced include not found: /app/skill/defold" before the
    image is ever produced. The pattern set is pinned here so the source cannot
    be dropped again without failing the gate.
    """
    assert DOCKERIGNORE.is_file()
    patterns = _dockerignore_patterns()

    # Match the leading segment as Docker would: "skill", "skill/", "skill/**",
    # "sk*" and a bare "*" all take the wheel's source back out of the context.
    offenders = [
        pattern
        for pattern in patterns
        if fnmatch.fnmatchcase("skill", pattern.split("/", 1)[0])
    ]
    assert not offenders, offenders

    # The de-cluttered context still drops the trees the wheel never reads.
    assert {"tests", "docs", "openwiki", ".github"} <= set(patterns)


def _action_step(
    document: dict, prefix: str, job: str | None = None
) -> dict:
    """Return the one step using an action whose reference starts ``prefix``."""
    steps = (
        document["jobs"][job].get("steps", []) if job else _steps(document)
    )
    matches = [
        step for step in steps if str(step.get("uses", "")).startswith(prefix)
    ]
    assert len(matches) == 1, f"{prefix}: {len(matches)} matching steps"
    return matches[0]


def test_ci_has_container_job() -> None:
    """CI builds the image with buildx and drives the real probe against it."""
    document = _load_workflow(CI_WORKFLOW)
    assert "container" in document["jobs"]

    # Buildx is the builder; build-push-action is the build. A shell
    # `docker build` would bypass the action's outputs and inputs.
    buildx = _action_step(document, "docker/setup-buildx-action@")
    assert buildx["uses"].count("@") == 1

    build = _action_step(document, "docker/build-push-action@")["with"]
    assert build["context"] == "."
    assert build["file"] == "Containerfile"
    # `push: false` keeps the tag local, and `load: true` puts the image in the
    # daemon so the following `docker run` probe can drive it.
    assert build["push"] is False
    assert build["load"] is True
    assert build["tags"] == "defold-mcp:ci"

    text = CI_WORKFLOW.read_text(encoding="utf-8")
    assert not re.search(r"^\s*run:\s*docker build\b", text, re.MULTILINE), (
        "the container job must build through docker/build-push-action"
    )
    assert "docker run -i --rm" in text
    # The proof drives the repository's own handshake probe, which asserts the
    # twenty-tool listing, a doctor call and stdout purity.
    assert "mcp_handshake_test.py" in text
    # `defold_doctor` resolves the cache dir, so it must be writable in-image.
    assert "DEFOLD_MCP_CACHE_DIR" in text
    # The image seeds that directory; the job proves a real write by uid 65532.
    assert "--entrypoint /usr/bin/python" in text


def test_release_image_workflow_is_gone() -> None:
    """No separate tag-triggered workflow: it could never fire.

    release-please creates the ``vX.Y.Z`` tag through the GitHub API with
    ``GITHUB_TOKEN``, and GitHub suppresses workflow runs that token raises, so
    a ``push: tags`` workflow would be correct but permanently dormant.
    Publishing lives in the release run instead; a reintroduced file fails here.
    """
    assert not RELEASE_IMAGE_WORKFLOW.exists()
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in WORKFLOWS
    ).lower()
    assert "release-image" not in text


def test_release_workflow_publishes_the_image_in_the_release_run() -> None:
    """The release run pushes the image with the token it already has."""
    document = _load_workflow(RELEASE_WORKFLOW)

    # Still one trigger: a push to master. Publishing hangs off that run rather
    # than a new trigger, which is what makes `GITHUB_TOKEN` sufficient.
    assert document["on"] == {"push": {"branches": ["master"]}}
    assert "pull_request" not in document["on"]

    release = document["jobs"]["release-please"]
    publish = document["jobs"]["publish-image"]

    # release-please creates the tag with the workflow token; the only
    # downstream consumer is a job in this same run, so no PAT is needed.
    assert release["permissions"] == {
        "contents": "write",
        "issues": "write",
        "pull-requests": "write",
    }
    # Job outputs are the only way a `needs`-dependent job reads step outputs.
    outputs = release["outputs"]
    assert outputs["releases_created"] == (
        "${{ steps.release.outputs.releases_created }}"
    )
    assert outputs["version"] == "${{ steps.release.outputs.version }}"
    assert outputs["tag_name"] == "${{ steps.release.outputs.tag_name }}"

    assert publish["needs"] == ["release-please"]
    # A run that only opened or refreshed the release PR has nothing to ship.
    # The full expression is pinned: `latest` is a literal raw tag that never
    # empties, so an inverted operator would push it from non-release runs and
    # no other assertion here would notice.
    assert publish["if"] == (
        "${{ needs.release-please.outputs.releases_created == 'true' }}"
    )
    assert publish["permissions"] == {
        "contents": "read",
        "packages": "write",
    }

    _action_step(document, "actions/checkout@", job="publish-image")
    _action_step(document, "docker/setup-buildx-action@", job="publish-image")

    login = _action_step(
        document, "docker/login-action@", job="publish-image"
    )["with"]
    assert login["registry"] == "ghcr.io"
    assert login["username"] == "${{ github.actor }}"
    assert login["password"] == "${{ secrets.GITHUB_TOKEN }}"

    metadata = _action_step(
        document, "docker/metadata-action@", job="publish-image"
    )["with"]
    assert metadata["images"] == "ghcr.io/${{ github.repository }}"
    tags = metadata["tags"]
    # The run is a branch push, so no tag ref exists for `semver` or `ref` to
    # read: the versions come from release-please's root-component outputs.
    assert "type=raw,value=${{ needs.release-please.outputs.version }}" in tags
    assert "type=raw,value=${{ needs.release-please.outputs.tag_name }}" in tags
    assert "type=raw,value=latest" in tags
    assert "github.ref" not in tags

    build = _action_step(
        document, "docker/build-push-action@", job="publish-image"
    )["with"]
    assert build["context"] == "."
    assert build["file"] == "Containerfile"
    assert build["push"] is True
    assert build["tags"] == "${{ steps.metadata.outputs.tags }}"
    # The published image carries the OCI labels metadata-action derives.
    assert build["labels"] == "${{ steps.metadata.outputs.labels }}"


IDENTIFIABLE = ("/home/yasser", "yasser", "worktree/")


def test_docs_have_no_identifiable_paths() -> None:
    """Committed documentation carries placeholders, never a local path.

    ``skill/`` ships with the repository and ``openwiki/`` is regenerated, so
    both are as public as ``docs/``; scanning them here means a regression
    fails the gate rather than reaching a reader.
    """
    files = (
        sorted((REPO_ROOT / "docs").rglob("*.md"))
        + sorted((REPO_ROOT / "skill").rglob("*.md"))
        + sorted((REPO_ROOT / "openwiki").rglob("*.md"))
        + sorted(REPO_ROOT.glob("*.md"))
    )
    assert files, "no documentation to scan"

    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for path in files
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if any(token in line for token in IDENTIFIABLE)
    ]
    assert not offenders, offenders

