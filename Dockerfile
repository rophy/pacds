FROM python:3.12-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PATH=/app/.venv/bin:$PATH HOME=/tmp
USER 10001
EXPOSE 8080
CMD ["pacds"]

# In-cluster test runner (k8s/dev/test-runner.yaml): the app plus dev dependencies, tests and kubectl.
FROM runtime AS test-runner
USER root
ARG KUBECTL_VERSION=v1.35.0
ADD https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl /usr/local/bin/kubectl
RUN chmod 755 /usr/local/bin/kubectl && uv sync --frozen
COPY tests ./tests
USER 10001
CMD ["sleep", "infinity"]
