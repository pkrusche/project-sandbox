FROM python:3.11-slim

# uv is pinned to an exact tag and digest so the build cannot pull a mutable
# "latest" image into a layer that later handles workspace/agent credentials.
# To upgrade, bump the tag and replace the digest with the one printed by
# `docker buildx imagetools inspect ghcr.io/astral-sh/uv:<tag>`.
COPY --from=ghcr.io/astral-sh/uv:0.8.0@sha256:0000000000000000000000000000000000000000000000000000000000000000 /uv /usr/local/bin/uv

# Pre-populate the uv package cache so the agent can run `uv sync` / `uv run`
# inside the sandbox without reaching PyPI (blocked by the firewall at runtime).
# UID 1000 is the agent user created by the sandbox layers that follow.
COPY pyproject.toml uv.lock README.md /tmp/project-setup/
COPY src/ /tmp/project-setup/src/
RUN UV_CACHE_DIR=/opt/uv-cache uv sync \
        --frozen \
        --project /tmp/project-setup \
    && chown -R 1000:1000 /opt/uv-cache \
    && rm -rf /tmp/project-setup

ENV UV_CACHE_DIR=/opt/uv-cache
