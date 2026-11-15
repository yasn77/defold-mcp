# Installing and registering the `defold` MCP server

`defold-mcp` is a Model Context Protocol server for the Defold game engine. It
exposes twenty `defold_*` tools to any MCP-capable harness: project scaffolding,
structured resource mutation, compilation, headless test runs, runtime
inspection and screenshots, cross-platform bundling, and an offline
documentation index.

It works in two modes and needs no choice from you. If a Defold Editor is open
for the project, writes go through the Editor's HTTP API as transactions; if not,
the server compiles with `bob.jar` and runs `dmengine_headless`. Every response
reports which mode served it as `"backend": "editor"` or `"backend": "headless"`.

This guide covers installation, the pre-flight probe, registration for each
harness, the alternative run modes (`uvx` and a container image), and the
timeouts that matter more than anything else here.

## Prerequisites

* **Python 3.14** — the package declares `requires-python = ">=3.14"`.
* **Network access on first use** — the toolchain cache is roughly 428 MB.
* **No Node.js.** The server is pure Python, which is deliberate: nothing here
  depends on `npm`, and it runs on hosts where Node is unavailable.
* **Only for the optional run modes** — `uv` (for `uvx`) or Docker/Podman (for
  the container image). Neither is needed for the two install options below.

### Option A — mise and uv (recommended)

```bash
git clone https://github.com/yasn77/defold-mcp.git
cd defold-mcp
mise run setup        # creates .venv and installs the package with dev extras
mise run handshake    # pre-flight probe over raw stdio JSON-RPC
```

`mise.toml` pins Python 3.14.10 and uv 0.13.7, so the same command works on any
machine with mise installed.

### Option B — plain `pip`

```bash
python3.14 -m venv .venv
. .venv/bin/activate
pip install -e .
pip install "mcp[cli]" httpx pillow
```

`pip install -e .` alone installs the runtime dependencies declared in
`pyproject.toml` (`mcp`, `httpx`, `pillow`); the second line adds the SDK's CLI
extras, which are handy when debugging a transport by hand.

Either path ends with a console script at `.venv/bin/defold-mcp`. Use its
**absolute** path in every harness configuration: harnesses do not inherit your
shell's `PATH`, and a relative path resolves against whichever directory the
client happens to start in.

### Artifacts and the JDK

Nothing is downloaded at install time. The first `defold_setup_toolchain` call
fetches Defold 1.14.1 (`bob.jar`, `dmengine`, `dmengine_headless`, builtins and
the pinned `ref-doc.zip`), builds the offline documentation index, and installs
a private Temurin JDK 25 under `~/.cache/defold-mcp/jdk-25` when the host JDK is
older. `bob.jar` for 1.14.x is compiled for class-file version 69, so a host
JDK 21 (common when another tool needs it) cannot run it; the private JDK is
installed beside your own JDKs rather than replacing them.

The cache lives in `DEFOLD_MCP_CACHE_DIR`, defaulting to `~/.cache/defold-mcp`.
Pass it explicitly in every harness config — see the next section.

## Run via `uvx`

`uvx` (short for `uv tool run`) executes a Python console script from a
throwaway, isolated environment: no checkout, no virtualenv to activate, and
nothing left behind except uv's package cache. It is the quickest way to try the
server or to pin a harness to an unreleased revision.

```bash
uvx defold-mcp --help
```

That form installs the published package, and is available once `defold-mcp` is
on PyPI. Until then, or to run a specific revision, name the source with
`--from`:

```bash
# the tip of the default branch
uvx --from git+https://github.com/yasn77/defold-mcp defold-mcp

# a branch, tag or commit — anything after @ belongs to the URL
uvx --from git+https://github.com/yasn77/defold-mcp@master defold-mcp
uvx --from git+https://github.com/yasn77/defold-mcp@v0.1.0 defold-mcp
```

`--from` names the **package** to install; the trailing `defold-mcp` names the
**command** to run. The two are the same name here, and that two-token form is
what allows them to differ — `--from httpie http`, for instance. The git forms
need `git` on the `PATH`, because uv asks it for the source tree.

Two properties are worth knowing before you point a harness at `uvx`:

* **The environment is ephemeral; the cache is not.** Each run resolves `mcp`,
  `httpx` and `pillow` afresh into a temporary virtualenv (uv keeps the wheels
  cached, so this is fast after the first run), and the environment is discarded
  when the server exits. The 428 MB Defold toolchain cache lives outside it and
  must point somewhere persistent, or every session re-downloads Defold:

  ```bash
  DEFOLD_MCP_CACHE_DIR="$HOME/.cache/defold-mcp" \
    uvx --from git+https://github.com/yasn77/defold-mcp defold-mcp
  ```

  A project-local directory is equally valid, and keeps two Defold versions from
  fighting over one cache:

  ```bash
  DEFOLD_MCP_CACHE_DIR="$PWD/.defold-cache" \
    uvx --from git+https://github.com/yasn77/defold-mcp defold-mcp
  ```

* **The interpreter is uv's choice.** uv selects (or downloads) a managed CPython
  that satisfies `requires-python = ">=3.14"`, so a host with only Python 3.12
  still works. `--python 3.14` pins the decision.

For a harness, use `uvx` itself as the command. Most clients take a single
executable plus an argument list, which fits `uvx` exactly:

```json
{
  "mcpServers": {
    "defold": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/yasn77/defold-mcp", "defold-mcp"],
      "env": { "DEFOLD_MCP_CACHE_DIR": "<home>/.cache/defold-mcp" }
    }
  }
}
```

`uvx` must then be on the harness's `PATH`, and clients spawn servers with a
filtered environment: use the absolute path to the `uvx` binary (`command -v
uvx`) if in doubt. Pin the source (`@v0.1.0`, or `--from defold-mcp==0.1.0` once
published) when you want reproducible harness behaviour; a moving branch means
every session may run different code.

## Run via container

The repository ships a two-stage `Containerfile` built on Chainguard's Python
images. The result carries Python, the package and its three dependencies, and
nothing else: no shell, no package manager, and the server runs as uid 65532
(`nonroot`). Docker and Podman are both fine; the examples use `docker`.

### Build the image

```bash
docker build -f Containerfile -t defold-mcp .
```

```bash
podman build -f Containerfile -t defold-mcp .
```

The build fetches the Chainguard base layers and the PyPI dependencies, so it
needs network access once; the image that results downloads nothing when it
starts. Only `latest` and `latest-dev` are anonymously pullable from `cgr.dev`,
which is why the `FROM` lines carry no version suffix: the build stage instead
asserts `sys.version_info >= (3, 14)`, so a base image that regressed below the
package's floor fails the build rather than producing an image that cannot import
the code. Pin both stages by digest (`…@sha256:…`) if you need byte-identical
rebuilds: nothing pins the current `latest` tags today, so the digest rule is a
no-op, and only once they are pinned does Renovate's "Container base images"
group refresh them — in one grouped PR rather than one per stage.

### Serve stdio

```bash
docker run -i --rm --init \
  -v defold-mcp-cache:/home/nonroot/.cache/defold-mcp \
  defold-mcp
```

Three details are load-bearing:

* **`-i` is mandatory.** Stdio is the transport; without an open stdin the server
  sees EOF and exits.
* **`--init` gives the container a real init as PID 1.** A process that is PID 1
  receives no default signal action, so a bare server would ignore `SIGTERM` and
  `docker stop` would wait ten seconds before killing it. With `--init`, SIGTERM
  reaches the server and it shuts down cleanly.
* **The cache volume must be writable by uid 65532.** A fresh Docker named volume
  is: the image seeds `/home/nonroot/.cache/defold-mcp` owned by 65532, and Docker
  copies an image directory's ownership into an empty named volume. A bind mount
  from your home directory usually is not, and needs `chown 65532:65532` first.
  For a throwaway session, redirect the cache to a writable path instead:

  ```bash
  docker run -i --rm --init \
    -e DEFOLD_MCP_CACHE_DIR=/tmp/defold-mcp-cache \
    defold-mcp
  ```

  That is fine for a short session and wasteful across restarts, because the
  toolchain is re-downloaded each time.

To let the server work on your project, mount it and start there:

```bash
docker run -i --rm --init \
  -v "$PWD:/workspace" -w /workspace \
  -v defold-mcp-cache:/home/nonroot/.cache/defold-mcp \
  defold-mcp
```

The mount must be writable by uid 65532 as well: resource mutations, build output
and bundles are written into the project tree. On Linux, run as yourself with
`--user "$(id -u):$(id -g)"` and point `DEFOLD_MCP_CACHE_DIR` at a directory that
uid can write, since `/home/nonroot` belongs to uid 65532 and no longer resolves
to a writable home.

### Serve StreamableHTTP

```bash
docker run --rm --init -p 9810:9810 \
  -v defold-mcp-cache:/home/nonroot/.cache/defold-mcp \
  defold-mcp --transport streamable-http --host 0.0.0.0 --port 9810
```

The endpoint is `http://127.0.0.1:9810/mcp`. `--host 0.0.0.0` is required here:
the default `127.0.0.1` binds the container's own loopback, which the published
port cannot reach. The transport has no authentication, and the tools write files
and download artifacts, so publish the port only on a trusted host or behind a
proxy.

### What the image cannot do

* **No shell, no `pip`, no `apk`.** `docker run --rm defold-mcp /bin/sh` fails by
  design. Diagnose with the handshake probe, or build the `-dev` variant for a
  one-off debugging session.
* **No JVM.** `defold_compile`, `defold_test_run` and `defold_bundle` run
  `bob.jar`, which needs JDK 25. `defold_setup_toolchain` installs a private
  Temurin JDK into the cache, so those tools work — but only once the toolchain
  has been downloaded, which needs network access and a persistent cache.
* **No virtual X server.** `defold_run(xvfb=true)` needs Xvfb, which the image
  does not carry. The headless engine and editor-backed screenshots are
  unaffected.

CI proves the image end to end: the `container` job in
`.github/workflows/ci.yml` builds it, writes into the default cache directory as
uid 65532, and runs `scripts/mcp_handshake_test.py` against `docker run -i
--rm`, so the initialize handshake, the twenty-tool listing, a `defold_doctor`
call and a clean SIGTERM exit are all verified inside the container on every
push.

## The canonical stdio configuration

Every harness below expresses the same thing. This is the reference form:

```json
{
  "mcpServers": {
    "defold": {
      "command": "<repo>/.venv/bin/defold-mcp",
      "args": [],
      "env": {
        "DEFOLD_MCP_CACHE_DIR": "<home>/.cache/defold-mcp"
      }
    }
  }
}
```

`<repo>` stands for the absolute path of your checkout and `<home>` for your home
directory — for example `/srv/defold-mcp` and the path `cd ~ && pwd` prints. Both
values must be absolute: harnesses do not expand `~`, and neither does the
server, so a `~` in `DEFOLD_MCP_CACHE_DIR` creates a directory literally named
`~`. The same block works for the other run modes: replace `command` (and `args`)
with the `uvx` invocation or the `docker run -i --rm --init …` form from the
sections above.

The `env` block is not decoration: most clients spawn servers with a filtered
environment, so an unexported `DEFOLD_MCP_CACHE_DIR` would silently send the
server to a different cache than the one your other tools populated.

The server also accepts `--transport streamable-http --host 127.0.0.1 --port
9810` for HTTP-capable clients (endpoint `http://127.0.0.1:9810/mcp`), and
`--log-level DEBUG` for diagnosis. Under the default stdio transport, stdout
carries **only** JSON-RPC frames; all logging goes to stderr.

## Pre-flight probe

Prove the raw protocol before editing any client configuration:

```bash
python scripts/mcp_handshake_test.py         # or: mise run handshake
python scripts/mcp_handshake_test.py /path/to/defold-mcp
```

The probe spawns the server with a sanitised environment (`PATH`, `HOME`, `USER`,
`TMPDIR` only, as a harness would), performs `initialize` with protocol version
`2025-06-18`, sends `notifications/initialized`, lists tools, calls
`defold_doctor`, then sends `SIGTERM` and requires an exit within five seconds.
It fails on any non-JSON line on stdout, on a tool count other than twenty, or
on a doctor call that reports an error.

A healthy run looks like this:

```text
binary            <repo>/.venv/bin/defold-mcp
environment       HOME, PATH, USER
initialize        protocolVersion 2025-06-18, server 'defold'
tools/list        20 tools, descriptions and schemas present
defold_doctor     ok=true, cache_dir <home>/.cache/defold-mcp
SIGTERM           exited in 0.01s (limit 5s)
stdout            3 JSON-RPC frames, no stray text
PASS
```

If this passes, a failure after registration is a client configuration problem,
not a server problem — which halves the search space.

## Registration per harness

| Harness | Where | Shape |
|---|---|---|
| Claude Code | `claude mcp add`, or `.mcp.json` | `mcpServers` |
| OpenAI Codex CLI | `~/.codex/config.toml` | `[mcp_servers.defold]` |
| Cursor | `.cursor/mcp.json` or `~/.cursor/mcp.json` | `mcpServers` |
| VS Code (Copilot agent mode) | `.vscode/mcp.json` | `servers` (not `mcpServers`) |
| Gemini CLI | `gemini mcp add`, or `~/.gemini/settings.json` | `mcpServers` |
| OpenCode | `opencode.json` | `mcp.defold`, `"type": "local"` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | `mcpServers` |
| Zed | `~/.config/zed/settings.json` | `context_servers` |
| Any HTTP client | `--transport streamable-http` | `http://127.0.0.1:9810/mcp` |

### Claude Code

```bash
claude mcp add defold \
  -e DEFOLD_MCP_CACHE_DIR="$HOME/.cache/defold-mcp" \
  -- <repo>/.venv/bin/defold-mcp
```

Or commit a project-scoped `.mcp.json` at the repository root using the canonical
block above plus a per-server timeout. Project scope is usually better: it keeps
the game project and its tooling in one review.

```json
{
  "mcpServers": {
    "defold": {
      "command": "<repo>/.venv/bin/defold-mcp",
      "args": [],
      "env": { "DEFOLD_MCP_CACHE_DIR": "<home>/.cache/defold-mcp" },
      "timeout": 900000
    }
  }
}
```

`timeout` is in milliseconds and covers one tool call; a value below 1000 is
ignored. Without it, a call is bounded only by `MCP_TOOL_TIMEOUT` (about 28 hours)
and by the idle window, which is 30 minutes for stdio servers.

### OpenAI Codex CLI

`~/.codex/config.toml`:

```toml
[mcp_servers.defold]
command = "<repo>/.venv/bin/defold-mcp"
args = []
startup_timeout_sec = 30
tool_timeout_sec = 900

[mcp_servers.defold.env]
DEFOLD_MCP_CACHE_DIR = "<home>/.cache/defold-mcp"
```

Both timeouts matter here: `startup_timeout_sec` covers the Python import, and
`tool_timeout_sec` must be generous because bundling and toolchain setup take
minutes.

### Cursor

`.cursor/mcp.json` in the project (or `~/.cursor/mcp.json` for every project):

```json
{
  "mcpServers": {
    "defold": {
      "command": "<repo>/.venv/bin/defold-mcp",
      "args": [],
      "env": { "DEFOLD_MCP_CACHE_DIR": "<home>/.cache/defold-mcp" }
    }
  }
}
```

Cursor's tool budget is modest; twenty tools fit comfortably, but avoid adding
several large servers to the same project.

### VS Code and Copilot agent mode

`.vscode/mcp.json` — note the top-level key is **`servers`**, and each entry
declares its transport:

```json
{
  "servers": {
    "defold": {
      "type": "stdio",
      "command": "<repo>/.venv/bin/defold-mcp",
      "args": [],
      "env": { "DEFOLD_MCP_CACHE_DIR": "<home>/.cache/defold-mcp" }
    }
  }
}
```

### Gemini CLI

```bash
gemini mcp add -s user \
  -e DEFOLD_MCP_CACHE_DIR="$HOME/.cache/defold-mcp" \
  defold <repo>/.venv/bin/defold-mcp
```

Or the equivalent `mcpServers` entry in `~/.gemini/settings.json`.

### OpenCode

`opencode.json`, inside the existing `mcp` object — OpenCode takes the command as
an **array** and requires a type:

```json
{
  "mcp": {
    "defold": {
      "type": "local",
      "command": ["<repo>/.venv/bin/defold-mcp"],
      "environment": { "DEFOLD_MCP_CACHE_DIR": "<home>/.cache/defold-mcp" },
      "timeout": 900000,
      "enabled": true
    }
  }
}
```

OpenCode measures `timeout` in milliseconds per server request and defaults to
5000, which is far too short for a toolchain download — set it explicitly. Note
that OpenCode calls the environment block `environment`, where most other clients
call it `env`.

Add the skill as well, so the agent gets the workflow rather than only the tools:

```bash
mkdir -p ~/.config/opencode/skills
ln -sfn <repo>/skill/defold ~/.config/opencode/skills/defold
```

A symlink is intentional: the repository stays the single source of truth, so a
`git pull` updates the skill in place.

### Windsurf

`~/.codeium/windsurf/mcp_config.json`, using the canonical `mcpServers` block.

### Zed

Zed nests servers under `context_servers`, not `mcpServers`:

```json
{
  "context_servers": {
    "defold": {
      "command": {
        "path": "<repo>/.venv/bin/defold-mcp",
        "args": [],
        "env": { "DEFOLD_MCP_CACHE_DIR": "<home>/.cache/defold-mcp" }
      }
    }
  }
}
```

### StreamableHTTP clients

```bash
<repo>/.venv/bin/defold-mcp \
  --transport streamable-http --host 127.0.0.1 --port 9810
```

The endpoint is `http://127.0.0.1:9810/mcp`. The server exposes no separate
health route; a POSTed `initialize` is the check:

```bash
curl -s -X POST http://127.0.0.1:9810/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

Bind to loopback. The HTTP transport has no authentication, and the tools can
write files inside the project root and download artifacts.

## Timeouts — read this before the first build

`defold_setup_toolchain`, `defold_bundle` and `defold_test_run` legitimately take
minutes: hundreds of megabytes of downloads, a JDK unpack, a first platform
package fetch, and cloud Extender compiling native extensions. Several clients
default to a 60-second per-call timeout and will kill the call mid-build, leaving
a half-written cache that the next call has to repair.

Set the client's per-tool timeout to at least **600 seconds**, and preferably
**900**. Where the knob lives differs by client:

| Harness | Setting | Notes |
|---|---|---|
| Codex CLI | `tool_timeout_sec = 900` in `~/.codex/config.toml` | Seconds; `startup_timeout_sec = 30` covers the import. |
| Claude Code | `"timeout": 900000` on the `.mcp.json` entry | Milliseconds. `MCP_TOOL_TIMEOUT=900000` sets it for every server; the idle window for stdio servers is 30 minutes. |
| OpenCode | `"timeout": 900000` on the `mcp` entry | Milliseconds; the default is 5000. |
| Cursor, VS Code, Gemini CLI, Zed | no documented per-tool knob | VS Code has an open feature request for one. These clients wait for the reply, so the practical rules are: one long call at a time, and do not interrupt a build that has already printed progress. |

The server returns each tool result in a single response, so a long call is
bounded by the client's timeout rather than by the server. What keeps a slow run
observable is `defold_logs`: a running engine's output — including a `bob` build
started by `defold_run` — is retained in a ring buffer with absolute offsets, so
you can poll it from a second call instead of waiting blind.

## Verifying the registration

In a **new** client session (clients read MCP configuration at start-up only):

1. Ask the agent to run `defold_doctor`. The reply is JSON with `ok: true`, a
   `java` block, `cache_dir`, and — for a project root — the backend and
   dependency state.
2. Run `defold_setup_toolchain` once. Expect roughly 428 MB in the cache and a
   `docs` section reporting per-scope document counts.
3. Run `defold_project_info` inside a Defold project. The `backend` field says
   which mode is active.

Tool names may be prefixed by the client (`mcp__defold__defold_doctor`,
`defold__defold_compile`, `mcp_defold_defold_run`). That is normal; the skill and
all documentation use the bare names and each harness resolves its own prefix.

For a container registration, run the same three checks and confirm the reported
`cache_dir` is the mounted volume's path. If it reports a path under `/tmp` or
`/home/nonroot`, the volume or `DEFOLD_MCP_CACHE_DIR` is missing and every new
container will re-download the toolchain.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Server does not appear in the client | JSON/TOML syntax error in the config | Re-parse the file (`python -c 'import json,sys; json.load(open(sys.argv[1]))' opencode.json`). A stray comma silently disables every server in the block. |
| `UnsupportedClassVersionError` from `bob.jar` | Host JDK older than 25 | `defold_setup_toolchain(install_jdk25=true)` installs a private JDK 25 in the cache. |
| `defold_bundle` or `defold_setup_toolchain` times out | Client's 60 s default | Raise the per-tool timeout to 600–900 s. |
| Artifacts are re-downloaded every session | Filtered environment drops `DEFOLD_MCP_CACHE_DIR` | Pass the `env` block explicitly. Under the container, mount the cache volume. |
| `defold_docs_search` returns nothing | An interrupted ingest left an empty `docs.db` | Call `defold_setup_toolchain` again; the server detects an empty index and rebuilds it. |
| Two gates or builds at once corrupt the virtualenv | `mise run test` and `mise run live` both run `uv venv --clear` | Run them **sequentially**, never concurrently. |
| `defold_preview` reports "editor not running" | It needs a live Editor (`GET /preview/{path}`) | Open the project in the Editor, or use `defold_run` plus `defold_runtime_act(action="screenshot")`. |
| Interactive `dmengine` fails with an X11/GLX error on a headless host | No display | Builds and tests are fine (`dmengine_headless` needs no display); for screenshots use `defold_run(xvfb=true)` where Xvfb is installed. |
| The container exits the moment it is started | `docker run` without `-i` | Stdio needs an open stdin: add `-i`. |
| `docker stop` takes ten seconds, then kills the server | No init, so PID 1 ignores SIGTERM | Run with `--init`. |
| The container cannot write its cache | The volume or bind mount is owned by another uid | A named volume works as-is; `chown 65532:65532` a bind mount, or run `-e DEFOLD_MCP_CACHE_DIR=/tmp/defold-mcp-cache`. |

Diagnostic habits that save time:

* Run the handshake probe first, always. It distinguishes "server is broken" from
  "client is misconfigured" in under a second.
* Read stderr, not stdout: the stdio transport owns stdout, and every log line —
  including tracebacks — is on stderr.
* Check the cache before blaming the network: `ls ~/.cache/defold-mcp` should show
  `bob.jar`-bearing version directories, `jdk-25`, `builtins.zip`, `ref-doc.zip`
  and `docs.db`.

## Uninstalling

Remove the `defold` entry from the client configuration, delete the skill symlink
(`rm ~/.config/opencode/skills/defold`), and delete the cache
(`rm -rf ~/.cache/defold-mcp`). Nothing is installed outside the checkout and the
cache directory.

For the other run modes: `uvx` leaves only uv's shared package cache
(`uv cache clean` empties it), and the image is removed with `docker image rm
defold-mcp` together with its volume (`docker volume rm defold-mcp-cache`).
