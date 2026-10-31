# One image, four roles: api | worker | scheduler | mcp (plus migrate/seed/reindex/backfill one-offs).
# Multi-stage: dependencies are resolved with uv from the lockfile in a build stage, and only the
# virtualenv plus runtime files reach the slim final image, which runs as a non-root user.

FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first (cached unless the lockfile changes), then the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/asmipahwa/signal-scout" \
      org.opencontainers.image.description="Signal Scout: AI Competitor Insights Pipeline" \
      org.opencontainers.image.licenses="MIT"
RUN useradd --create-home --uid 10001 scout
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY seeds ./seeds
COPY docker/entrypoint.sh /usr/local/bin/entrypoint
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 LLM_STATE_DIR=/app/.state
RUN mkdir -p /app/.state && chown scout /app/.state
USER scout
EXPOSE 8000 8001 9100
# Liveness for the API role; other roles override or ignore it in compose.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=4)" || exit 1
ENTRYPOINT ["entrypoint"]
CMD ["api"]
