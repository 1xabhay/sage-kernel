FROM python:3.15.0-slim-bookworm@sha256:0e019bbdfb5d9874cb2389f1c2884b64aa2ac97c99960b5bc521cef978c0617d
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SAGE_STATE_DIR=/data SAGE_DEVICE_BIND=0.0.0.0
WORKDIR /app
RUN mkdir /data && chown 1000:1000 /data && chmod 700 /data
COPY sage ./sage
COPY tests ./tests
COPY conformance ./conformance
COPY evals ./evals
USER 1000:1000
CMD ["python", "-m", "sage"]
