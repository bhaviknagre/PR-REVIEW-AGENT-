# syntax=docker/dockerfile:1
# One image, three roles chosen by command: api | worker | migrate.
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS build
COPY --from=ghcr.io/astral-sh/uv:0.5.14@sha256:f0786ad49e2e684c18d38697facb229f538a6f5e374c56f54125aabe7d14b3f7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
RUN useradd --system --uid 10001 --no-create-home --home-dir /nonexistent app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
# alembic reads script_location relative to /app, so the migrations live there too
COPY src/pr_review/storage/migrations ./src/pr_review/storage/migrations
COPY <<'SH' /usr/local/bin/entry
#!/bin/sh
set -e
case "$1" in
  api)     exec uvicorn pr_review.api.app:create_app --factory --host 0.0.0.0 --port 8000 ;;
  worker)  exec arq pr_review.pipeline.queue.WorkerSettings ;;
  migrate) exec alembic upgrade head ;;
  *)       exec "$@" ;;
esac
SH
RUN chmod 755 /usr/local/bin/entry
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1
ENTRYPOINT ["entry"]
CMD ["api"]
