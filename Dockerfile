# Adhar AI — one image, eight published names.
#
# The platform package runs eight Deployments whose images are
# ghcr.io/adhar-io/adhar-ai-{runtime,mcp-<domain>}:latest. They are all the SAME
# image: the role comes from the container `args` the manifests already pass
# (`mcp --domain=<d> --listen=:8080`, `runtime --config=… --listen=:8080`).
# CI pushes this build under every name, so no manifest change is needed.
#
# There is no `gateway` name: ADR-0025 retired adhar-ai-llm-gateway in favour of
# the upstream agentgateway proxy. The `gateway` subcommand still exists in the
# CLI for local development (docker-compose), but nothing deploys it.
#
# Non-root 65532 with a read-only root filesystem, matching the manifests'
# securityContext (only /tmp is writable, mounted as an emptyDir).

# ---------------------------------------------------------------- builder ----
FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer first so source edits do not invalidate the wheel cache.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra rag

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra rag

# ----------------------------------------------------------------- runtime ---
FROM python:3.12-slim

LABEL org.opencontainers.image.title="adhar-ai" \
      org.opencontainers.image.description="Adhar AI — MCP tool servers and the GitOps-safe agent runtime (ADR-0024)" \
      org.opencontainers.image.source="https://github.com/adhar-io/adhar-ai" \
      org.opencontainers.image.licenses="Apache-2.0" \
      adhar.io/origin="adhar-ai"

# libpq is needed by psycopg for the pgvector RAG store.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 nonroot \
    && useradd --uid 65532 --gid 65532 --home-dir /home/nonroot --create-home nonroot

COPY --from=builder --chown=65532:65532 /app/.venv /app/.venv
COPY --from=builder --chown=65532:65532 /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/nonroot

WORKDIR /app
USER 65532:65532
EXPOSE 8080

ENTRYPOINT ["adhar-ai"]
CMD ["runtime", "--listen=:8080"]
