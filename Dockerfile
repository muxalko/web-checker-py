# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.11.4
FROM python:${PYTHON_VERSION}-slim AS base

# Prevents Python from writing pyc files.
ENV PYTHONDONTWRITEBYTECODE=1

# Keeps Python from buffering stdout and stderr to avoid situations where
# the application crashes without emitting any logs due to buffering.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Create a non-privileged user that the app will run under.
# See https://docs.docker.com/go/dockerfile-user-best-practices/
ARG UID=10001
RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/nonexistent" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "${UID}" \
    appuser

# Named volumes mounted here inherit ownership that permits the non-root checker
# to create and update its SQLite database.
RUN install -d -o appuser -g appuser /data

FROM base AS production

# Production installs an immutable copy without development-only tooling.
COPY --chown=appuser:appuser pyproject.toml README.md ./
COPY --chown=appuser:appuser mock_site/ ./mock_site/
COPY --chown=appuser:appuser web_checker/ ./web_checker/
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install .

USER appuser
CMD ["web-checker", "--help"]

FROM base AS development

# Development keeps editable installation and the repository's test tools.
COPY --chown=appuser:appuser . .
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --editable ".[dev]"

USER appuser
CMD ["web-checker", "--help"]
