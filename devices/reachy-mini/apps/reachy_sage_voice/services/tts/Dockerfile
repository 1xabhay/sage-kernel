# Pocket TTS on CPU (app.py). The weights and the one voice are baked in at build time and the
# server runs offline, so it starts in seconds and the read-only root never needs to write.
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/models/hf
WORKDIR /app
COPY requirements.lock ./
# torch's CPU build lives on its own index (requirements.lock pins it by hash).
RUN pip install --no-cache-dir --disable-pip-version-check --require-hashes \
    --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.lock
COPY app.py ./
RUN python -c "import app; app.fetch()"
ENV HF_HUB_OFFLINE=1 HOME=/tmp
COPY test_app.py ./
USER 1000:1000
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
