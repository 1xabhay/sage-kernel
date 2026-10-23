# syntax=docker/dockerfile:1
# Essentia's TensorFlow wheels are published for linux/amd64; on Apple Silicon
# Docker will emulate. For native-speed local runs, prefer `make setup-mood`.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TF_CPP_MIN_LOG_LEVEL=3 \
    MUSIC_TELLING_CACHE=/cache \
    UV_LINK_MODE=copy

# libsndfile/ffmpeg give librosa a decoder for the preview MP3s; Essentia
# statically links its own, but librosa on slim images needs the system one.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer: copy only what affects resolution so edits don't bust it.
COPY pyproject.toml README.md ./
RUN uv venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH"

FROM base AS full
# Mood models add TensorFlow; build the `lite` target to skip them.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install -e ".[dev,mood]" || uv pip install -e ".[dev]"
COPY src ./src
COPY tests ./tests
RUN mkdir -p /cache /app/out
ENTRYPOINT ["music-telling"]
CMD ["doctor"]

FROM base AS lite
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install -e ".[dev]"
COPY src ./src
COPY tests ./tests
RUN mkdir -p /cache /app/out
ENTRYPOINT ["music-telling"]
CMD ["doctor"]
