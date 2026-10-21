FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SAGE_STATE_DIR=/data SAGE_DEVICE_BIND=0.0.0.0
WORKDIR /app
RUN mkdir /data && chown 1000:1000 /data && chmod 700 /data
COPY sage ./sage
COPY tests ./tests
COPY conformance ./conformance
COPY evals ./evals
USER 1000:1000
CMD ["python", "-m", "sage"]
