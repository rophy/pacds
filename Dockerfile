# Base images are build arguments so a corporate build can pull them from its own registry:
#   docker build --build-arg PYTHON_IMAGE=registry.corp.example/python:3.12-slim \
#                --build-arg UV_IMAGE=registry.corp.example/astral-sh/uv:0.11.6 -t pacds .
# Python packages come from the URLs pinned in uv.lock (PyPI): build where PyPI is reachable, directly or through
# Docker's predefined HTTPS_PROXY build argument, then push the image to the internal registry.
# The default pulls Docker Hub through Google's mirror, which avoids Docker Hub pull rate limits.
ARG PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.6
FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Development and evaluation only (llm.api claude_code): the Claude Code CLI, which runs on a subscription's token
# (CLAUDE_CODE_OAUTH_TOKEN). Empty by default, so the production image does not contain it.
ARG CLAUDE_CODE_VERSION=
RUN if [ -n "$CLAUDE_CODE_VERSION" ]; then \
      apt-get update && apt-get install -y --no-install-recommends curl \
      && curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_CODE_VERSION" \
      && install -m 0755 "$(readlink -f /root/.local/bin/claude)" /usr/local/bin/claude \
      && rm -rf /root/.local /root/.claude /root/.claude.json \
      && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*; \
    fi
ENV DISABLE_AUTOUPDATER=1
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
# The support-agent sample, with real files where the repository has a symlink (COPY does not follow it).
COPY samples/support-agent /opt/pacds/samples/support-agent
COPY src/pacds_eval/skills /opt/pacds/samples/support-agent/skills
RUN uv sync --frozen --no-dev
ENV PATH=/app/.venv/bin:$PATH HOME=/tmp
# Evaluation defaults (docs/evaluation-runbook.md): the case set mounted at /cases, run directories under /runs, and
# a deployed PACDS's traces at /traces when mounted (pacds eval run collects from there).
ENV PACDS_CASES_DIR=/cases PACDS_RUNS_DIR=/runs
ARG PACDS_VERSION=
ENV PACDS_VERSION=$PACDS_VERSION
# The git cache volume (deploy/compose.yaml) takes this directory's owner when Docker first creates it.
RUN mkdir -p /var/cache/pacds && chown 10001 /var/cache/pacds
USER 10001
EXPOSE 8080
ENTRYPOINT ["pacds"]

