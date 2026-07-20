FROM python:3.11-slim

ARG AGENT_UID=1000
ARG AGENT_GID=1000

# uv is pinned to an exact tag and digest so the build cannot pull a mutable
# "latest" image into a layer that later handles workspace/agent credentials.
# To upgrade, bump the tag and replace the digest with the one printed by
# `docker buildx imagetools inspect ghcr.io/astral-sh/uv:<tag>`.
COPY --from=ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc /uv /usr/local/bin/uv

# Pre-populate the uv package cache so the agent can run `uv sync` / `uv run`
# inside the sandbox without reaching PyPI (blocked by the firewall at runtime).
# Two layers: the deps-only layer only rebuilds when pyproject.toml/uv.lock
# change, while the slower project-install layer rebuilds on every source edit
# but reuses the dependency cache already populated above.
COPY pyproject.toml uv.lock /tmp/project-setup/
RUN UV_CACHE_DIR=/opt/uv-cache uv sync \
        --frozen \
        --no-install-project \
        --project /tmp/project-setup

# README.md is declared as `readme =` in pyproject.toml, so the build backend
# needs it present once the project itself is installed below.
COPY README.md /tmp/project-setup/
COPY src/ /tmp/project-setup/src/
# Match ownership to the agent user created by the sandbox layers that follow.
RUN UV_CACHE_DIR=/opt/uv-cache uv sync \
        --frozen \
        --project /tmp/project-setup \
    && chown -R "${AGENT_UID}:${AGENT_GID}" /opt/uv-cache \
    && rm -rf /tmp/project-setup

ENV UV_CACHE_DIR=/opt/uv-cache
