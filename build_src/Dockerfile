# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.10.8 AS uv

FROM python:3.14-slim

COPY --from=uv /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

# Keep dependency installation cached until their definitions change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-default-groups --no-install-project

# The application has no need for root privileges at runtime.
RUN groupadd --system app && useradd --system --gid app --home-dir /app app
COPY --chown=app:app src/ ./src/
COPY --chown=app:app build_src/docker_entrypoint.py ./build_src/docker_entrypoint.py

USER app

ENTRYPOINT ["python", "/app/build_src/docker_entrypoint.py"]
