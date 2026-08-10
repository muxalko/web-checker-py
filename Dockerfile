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

# Copy the project before installation. The project metadata in pyproject.toml is
# the canonical dependency definition.
COPY --chown=appuser:appuser . .

# Install the application and development tools used by the current prototype.
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --editable ".[dev]"

# Switch to the non-privileged user to run the application.
USER appuser

# Default to the actual application CLI. Compose overrides this for the mock site.
CMD ["web-checker", "--help"]
