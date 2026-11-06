# SearXNG with this folder's settings.yml baked in: nothing is mounted from the host.
# Upstream declares VOLUME /etc/searxng, and a recreate keeps the old volume over
# anything baked there, so our settings live outside it and SEARXNG_SETTINGS_PATH points at them.
FROM searxng/searxng:2026.10.19-81371861f@sha256:5ed0323b5f5f8701264ef65e239a2bd83cb57098f2b971fa30bf79c06eeae271
COPY settings.yml /usr/local/searxng/sage-settings.yml
ENV SEARXNG_SETTINGS_PATH=/usr/local/searxng/sage-settings.yml
