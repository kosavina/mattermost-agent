# syntax=docker/dockerfile:1

# Stage "uv": only a source for the uv binary. It is its own FROM line so that Dependabot,
# which reads FROM lines, can update the version and digest.
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

# Stage "build": installs the dependencies and the app into /app/.venv. uv, the lock file
# and the build caches stay in this stage and never reach the final image.
FROM python:3.15.2-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first: this layer is cached and rebuilt only when uv.lock or pyproject.toml change.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
# Then the app itself, as a regular (non-editable) install, so the venv needs no src/.
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Final stage: the same Python base (the venv points at its interpreter), the venv, and a
# non-root user. Nothing else.
FROM python:3.15.2-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && install -d -o 10001 -g 10001 -m 700 /data
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER 10001
# The first heartbeat is written at startup. The start period is for a slow machine.
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
    CMD ["mattermost-agent", "healthcheck"]
CMD ["mattermost-agent"]
