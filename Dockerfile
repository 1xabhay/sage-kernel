FROM python:3.14.7-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SAGE_STATE_DIR=/data SAGE_DEVICE_BIND=0.0.0.0
WORKDIR /app
RUN mkdir /data && chown 1000:1000 /data && chmod 700 /data
COPY sage ./sage
COPY tests ./tests
COPY conformance ./conformance
COPY evals ./evals
USER 1000:1000
CMD ["python", "-m", "sage"]
