# syntax=docker/dockerfile:1
# Essentia's TensorFlow wheels are published for linux/amd64; on Apple Silicon
# Docker will emulate. For native-speed local runs, prefer `make setup-mood`.
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TF_CPP_MIN_LOG_LEVEL=3 \
    MUSIC_TELLING_CACHE=/cache \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# libsndfile/ffmpeg give librosa a decoder for the preview MP3s; Essentia
# statically links its own, but librosa on slim images needs the system one.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.5-python3.12-bookworm@sha256:19a8c92b461bbc32e8bd30c15132cec1d16c49c61f4359c9225262938485f513 \
     /usr/local/bin/uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer: only what affects resolution, so code edits don't bust it.
COPY pyproject.toml uv.lock README.md ./

FROM base AS full
# Mood models add TensorFlow; build the `lite` target to skip them.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --extra mood
COPY src ./src
COPY tests ./tests
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra mood && mkdir -p /cache /app/out
ENTRYPOINT ["music-telling"]
CMD ["doctor"]

FROM base AS lite
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project
COPY src ./src
COPY tests ./tests
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked && mkdir -p /cache /app/out
ENTRYPOINT ["music-telling"]
CMD ["doctor"]
