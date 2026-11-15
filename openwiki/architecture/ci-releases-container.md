---
type: architecture
title: CI, releases and the container
description: The automation and delivery layer — Ubicloud-run PR checks, release-please semantic releases with GHCR image publication, Renovate native automerge, the Chainguard container and the skill/integration CLI subcommands.
tags: [defold, ci, github-actions, release, renovate, container]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T16:38:15.389Z
sources:
  - id: openwiki-source-164e2da859b5277df81c7d94
    resource: repo://.github/workflows/ci.yml
  - id: openwiki-source-4d1d392666be6dfdd7a91a2e
    resource: repo://.github/workflows/release.yml
  - id: openwiki-source-f7e7ee941b3c2ea549eb4de1
    resource: repo://.release-please-config.json
  - id: openwiki-source-8ea12a05c67ed54e49e49437
    resource: repo://Containerfile
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-cc2828785988d51226a4241e
    resource: repo://renovate.json
  - id: openwiki-source-1370c63fa3e69beb9835d3e9
    resource: repo://src/defold_mcp/__main__.py
  - id: openwiki-source-90b1741ee7f58a19f59c234d
    resource: repo://tests/test_workflows.py
generated: { by: "opencode", at: "2026-11-15T16:38:15.389Z" }
---

# CI, releases and the container

The final phases delivered the delivery story: how the server is checked on every PR, versioned and released, and shipped as a container and as installable CLI surface.

## CI (`ci.yml`) — Ubicloud-run, mise-driven

Three jobs, each on the smallest valid Ubicloud runner for its load (`ubicloud-standard-2`; the live smoke on `-4` for the JVM build):

- **unit** — `mise run test` (the 335-test hermetic gate, zero warnings)
- **handshake** — `mise run handshake` (the stdio probe: 20 tools, stdout purity, clean SIGTERM)
- **live** — `mise run live` (the real Defold 1.14.1 smoke) with an `actions/cache` keyed on the **literal Defold version** (no `restore-keys`) — the version-agnostic `builtins.zip`/`ref-doc.zip` would otherwise be silently served stale across a version bump.
- **container** — builds the image with buildx (`docker/setup-buildx-action` + `docker/build-push-action`, `push: false`/`load: true`) and drives the real handshake probe through `docker run -i --rm --init`, proving the MCP runs containerised.

Every action is SHA-pinned with a `# vN` comment; `mise` keeps CI identical to local (pinned python 3.14.10 + uv 0.13.7).

## Releases (`release.yml` + release-please)

`googleapis/release-please-action` (python strategy) maintains the `CHANGELOG.md` (Keep-a-Changelog), bumps the version in **both** `pyproject.toml` `[project] version` and `src/defold_mcp/__init__.py __version__` (the strategy's regex matches the literal), and on merge tags + releases. `bump-minor-pre-major: true` keeps the 0.x line from jumping to 1.0 on the first breaking change.

A second job, **`publish-image`**, runs in the *same* workflow (`needs: [release-please]`, gated on `releases_created == 'true'`) and pushes the container to `ghcr.io/<owner>/defold-mcp` with three tags — `:X.Y.Z`, `:vX.Y.Z`, `:latest` — derived from release-please's outputs. The same-run job is deliberate: release-please creates the tag with `GITHUB_TOKEN`, whose events never trigger a *new* workflow, so a separate `push: tags` workflow would be dormant; the same-run job needs no PAT.

## Renovate (`renovate.json`)

Native automerge (`automerge: true`, `automergeType: "pr"`, `automergeStrategy: "squash"`, `platformAutomerge: false`) — Renovate merges once checks pass, with no branch-protection requirement. Four logical groups (Python/pep621, GitHub Actions with `pinDigests`, mise toolchain, Container base images); majors and vulnerability alerts are **never** automerged (labelled, reviewed by hand). `:semanticCommitTypeAll(chore)` keeps dependency PRs out of the release-please versioning.

## The container (`Containerfile`)

Two-stage Chainguard build: `cgr.dev/chainguard/python:latest-dev` (which ships `pip`/`uv`) builds a venv and installs the package; the minimal `latest` runtime (no shell, `nonroot` uid 65532) receives the venv via `COPY --chown`, with a seeded cache directory so a mounted named volume stays writable by the runtime user, and `--init` at run time so the server (PID 1) forwards SIGTERM. `ENTRYPOINT ["defold-mcp"]` serves stdio by default.

## The CLI subcommands

`defold-mcp skill install [--target DIR]` copies the portable skill (bundled in the wheel via hatchling `force-include`) into the detected harness skill directories. `defold-mcp integration install [claude|opencode|codex|cursor|vscode|gemini|windsurf|zed|all] [--command …] [--dry-run]` registers the server per harness with backup, round-trip validation, idempotency and a resolved command — automating what `docs/INSTALL.md` documents.

The scheduled OpenWiki CI workflow was removed; the wiki is maintained manually through the OpenWiki lifecycle.
