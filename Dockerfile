# syntax=docker/dockerfile:1.7

# ── Builder stage ────────────────────────────────────────────────────────────
# Resolves the locked dependency tree into an isolated venv with uv so the
# runtime stage stays small (no build toolchain, no cache).
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv

WORKDIR /app

# Pre-install dependencies in a layer that only rebuilds when the lockfile
# or project metadata changes. ``--no-install-project`` skips the project
# itself; we copy the source and install it in a separate step.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ── Runtime stage ────────────────────────────────────────────────────────────
# Minimal image: copy the prebuilt venv and the application code. No package
# manager, no compiler. Runs as a non-root user.
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app/src"

RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder --chown=app:app /opt/venv /opt/venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --from=builder --chown=app:app /app/migrations /app/migrations
COPY --from=builder --chown=app:app /app/alembic.ini /app/alembic.ini

USER app

# Default command runs the worker. The API service overrides this via the
# compose ``command:`` field. Both share the same image.
CMD ["python", "-m", "meta_agent.worker"]
