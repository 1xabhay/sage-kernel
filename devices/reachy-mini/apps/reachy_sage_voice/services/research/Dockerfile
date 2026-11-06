# Song research (app.py): Shazam through shazamio, then MusicBrainz, Wikipedia, LRCLIB and the
# private SearXNG. No model and nothing to bake in; unlike stt and tts it needs the internet.
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --disable-pip-version-check --require-hashes -r requirements.lock
COPY app.py test_app.py ./
USER 1000:1000
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
