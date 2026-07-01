FROM python:3.11-slim

ARG AGENT_UID=1000
ARG AGENT_GID=1000

# uv is pinned to an exact tag and digest so the build cannot pull a mutable
# "latest" image into a layer that later handles workspace/agent credentials.
# To upgrade, bump the tag and replace the digest with the one printed by
# `docker buildx imagetools inspect ghcr.io/astral-sh/uv:<tag>`.
COPY --from=ghcr.io/astral-sh/uv:0.11.26@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 /uv /usr/local/bin/uv

# Pre-populate the uv package cache so the agent can run `uv sync` / `uv run`
# inside the sandbox without reaching PyPI (blocked by the firewall at runtime).
# Match ownership to the agent user created by the sandbox layers that follow.
COPY pyproject.toml uv.lock README.md /tmp/project-setup/
COPY src/ /tmp/project-setup/src/
RUN UV_CACHE_DIR=/opt/uv-cache uv sync \
        --frozen \
        --project /tmp/project-setup \
    && chown -R "${AGENT_UID}:${AGENT_GID}" /opt/uv-cache \
    && rm -rf /tmp/project-setup

ENV UV_CACHE_DIR=/opt/uv-cache
