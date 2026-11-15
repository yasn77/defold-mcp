# Containerfile for defold-mcp — a two-stage build on Chainguard's Python images.
#
# Why Chainguard: the images carry no shell or package manager in production,
# run as the unprivileged `nonroot` user (uid 65532) and are rebuilt daily, so
# the shipped layer has almost no attack surface and no distribution tooling
# for an exploit to reuse.
#
# Tag choice: only `latest` and `latest-dev` are anonymously pullable from
# cgr.dev. The version-suffixed tags (`3.14`, `3.14.10`, `3.14-dev`, …) exist
# but need a Chainguard account with access, so a pinned minor is not
# available here. As of this build `latest` tracks Python 3.15, comfortably
# above the package's `requires-python = ">=3.14"`; the build stage asserts
# that floor so a future tag moving backwards fails the build loudly rather
# than shipping an image that cannot import the package. Both stages use the
# same track, which keeps the venv's interpreter symlink valid. Nothing pins
# the current `latest` tags, so pin both `FROM` lines by digest (`…@sha256:…`)
# when you need byte-identical rebuilds: today the digest rule is a no-op, and
# only once pinned does Renovate's "Container base images" group refresh both
# in one grouped PR.
#
# Build:  docker build -f Containerfile -t defold-mcp .
# Stdio:  docker run -i --rm -v defold-mcp-cache:/home/nonroot/.cache/defold-mcp \
#           defold-mcp
# HTTP:   docker run --rm --init -p 9810:9810 defold-mcp --transport \
#           streamable-http --host 0.0.0.0

# --- build stage ---------------------------------------------------------
# `-dev` is the variant that ships pip, uv, a shell and root access.
FROM cgr.dev/chainguard/python:latest-dev AS build

# The dev variant defaults to `nonroot`; root is needed to own /app and the
# venv it holds. The copy into the runtime stage re-owns both to `nonroot`.
USER root

WORKDIR /app

# Fail here rather than at container start if the tag no longer satisfies the
# package's declared interpreter floor.
RUN python -c "import sys; assert sys.version_info[:2] >= (3, 14), sys.version"

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# The wheel is built from `src/defold_mcp`, so the context needs `pyproject.toml`,
# `src/` and `README.md`. `.dockerignore` drops the virtualenv, caches, VCS
# metadata and the documentation, test and tooling trees.
COPY . .

# A self-contained venv, so the runtime stage only needs Python plus this
# directory. The dependencies (`mcp`, `httpx`, `pillow`) come from PyPI here,
# at build time; nothing is fetched when the container starts.
#
# `cache-seed` is a marker directory: it exists only so the runtime stage can
# recreate the cache directory, owned by `nonroot`, with a single `COPY`. A
# `COPY` of an empty directory would carry nothing across, hence the `.keep`.
RUN python -m venv /app/venv \
    && /app/venv/bin/pip install --no-cache-dir . \
    && mkdir -p /app/cache-seed \
    && touch /app/cache-seed/.keep

# --- runtime stage -------------------------------------------------------
# The minimal variant: no shell, no pip, `nonroot` by default. Nothing below
# may use `RUN`, because there is nothing to run it with.
FROM cgr.dev/chainguard/python:latest

WORKDIR /app

# `--chown` matters: the venv was built by root, and the server writes its
# cache (and Python its bytecode caches) as `nonroot`.
COPY --chown=nonroot:nonroot --from=build /app/venv /app/venv

# Seed the cache directory instead of leaving the server to create it at
# start-up: Docker mounts a named volume over the path and gives a *missing*
# directory's ownership (root) to the volume root, leaving `nonroot` unable to
# write. An existing directory is what the volume inherits.
COPY --chown=nonroot:nonroot --from=build /app/cache-seed/ /home/nonroot/.cache/defold-mcp/

ENV PATH="/app/venv/bin:$PATH"

# Stated rather than inherited from the base image, so the intent survives a
# base-image change. `/home/nonroot` is the passwd home of uid 65532; the
# directory is seeded above with that ownership so a named volume over it stays
# writable. Mount the volume there to persist the toolchain cache, or point
# DEFOLD_MCP_CACHE_DIR at another writable path.
USER nonroot
ENV DEFOLD_MCP_CACHE_DIR=/home/nonroot/.cache/defold-mcp

# The console script is the entry point: `docker run -i … defold-mcp` serves
# stdio, and transport flags pass straight through.
ENTRYPOINT ["defold-mcp"]
CMD []
