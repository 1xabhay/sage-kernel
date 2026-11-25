FROM python:3.15.1-slim-bookworm@sha256:575f8bc12ec26542e0678bc6b60e385218248019d433157ec327264a78c3a853
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SAGE_STATE_DIR=/data SAGE_DEVICE_BIND=0.0.0.0
WORKDIR /app
RUN mkdir /data && chown 1000:1000 /data && chmod 700 /data
COPY sage ./sage
COPY tests ./tests
COPY conformance ./conformance
COPY evals ./evals
COPY bench/latency.py ./bench/latency.py
# The image is the same whatever the host's umask or checkout modes. A RUN, not a COPY flag: see test_deploy.
RUN chmod -R u=rwX,go=rX sage tests conformance evals bench
USER 1000:1000
CMD ["python", "-m", "sage"]
