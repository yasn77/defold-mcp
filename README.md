# defold-mcp

A Model Context Protocol server and agent skill for the [Defold](https://defold.com)
game engine. Twenty `defold_*` tools cover project scaffolding, structured
resource mutation, compilation, headless test runs, runtime inspection and
screenshots, cross-platform bundling, and an offline documentation index.

The server picks its own backend: an open Defold Editor serves writes through its
HTTP API as transactions, otherwise `bob.jar` and `dmengine_headless` do the
work. Every response reports which one answered, as `"backend": "editor"` or
`"backend": "headless"`.

## Run modes

| Mode | Command | Best for |
|---|---|---|
| mise + uv | `mise run setup` then `.venv/bin/defold-mcp` | Development and the full test suite |
| plain pip | `pip install .` then `.venv/bin/defold-mcp` | Hosts without mise |
| `uvx` | `uvx --from git+https://github.com/yasn77/defold-mcp defold-mcp` | Trying it, or pinning a harness to a revision, with no checkout |
| container | `docker build -f Containerfile -t defold-mcp .` | Reproducible, shell-less, non-root deployments |

All four serve the same stdio transport by default; `--transport
streamable-http` serves the same tools over HTTP.

## Quick start

```bash
git clone https://github.com/yasn77/defold-mcp.git
cd defold-mcp
mise run setup        # Python 3.14.10 and uv 0.13.7, pinned in mise.toml
mise run handshake    # raw stdio JSON-RPC probe: handshake, 20 tools, doctor, SIGTERM
```

Then register the absolute path of `.venv/bin/defold-mcp` with your harness and
give the server a generous per-tool timeout: the first toolchain download is
about 428 MB, and `defold_bundle` and `defold_test_run` legitimately take
minutes.

## Documentation

* [`docs/INSTALL.md`](docs/INSTALL.md) — installation, `uvx` and container run
  modes, registration for each harness, the pre-flight probe, timeouts and
  troubleshooting.
* [`skill/defold/SKILL.md`](skill/defold/SKILL.md) — the agent skill: the
  workflow to follow when building, testing or bundling a Defold game.

## Development

```bash
mise run test    # hermetic suite, live tests deselected
mise run live    # downloads the real Defold 1.14.1 toolchain and builds a game
```

Run the two gates **sequentially**: both rebuild `.venv`, and running them
concurrently corrupts it. CI (`.github/workflows/ci.yml`) runs the unit suite,
the stdio handshake, the live smoke and a container proof on Ubicloud runners.
