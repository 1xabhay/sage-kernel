"""Ollama backend - a local, free, open-weights model.

Ollama constrains generation to a JSON schema via the `format` field, which is
what makes small models usable for the structured stages of this pipeline.
"""

from __future__ import annotations

import os

import httpx2 as httpx

from .base import ProviderError

DEFAULT_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


class OllamaProvider:
    name = "ollama"

    def __init__(self, model: str, host: str = DEFAULT_HOST, num_ctx: int = 16384):
        self.model = model
        self.host = host.rstrip("/")
        # Ollama defaults to a 4096-token context, which silently truncates a
        # song's lyrics plus the theme brief. Set it explicitly.
        self.num_ctx = num_ctx
        self._client = httpx.Client(timeout=httpx.Timeout(600.0, connect=5.0))

    def complete(self, *, system: list[str], user: str, schema: dict, max_tokens: int) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "\n\n".join(system)},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema,
            "options": {
                "num_ctx": self.num_ctx,
                "num_predict": max_tokens,
                # Low but non-zero: deterministic enough to be reproducible,
                # warm enough that the story prose isn't wooden.
                "temperature": 0.4,
            },
        }
        try:
            resp = self._client.post(f"{self.host}/api/chat", json=payload)
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise ProviderError(
                f"Cannot reach Ollama at {self.host}. Start it with `ollama serve`."
            ) from exc
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:300]
            if exc.response.status_code == 404:
                raise ProviderError(
                    f"Model '{self.model}' not found. Pull it: `ollama pull {self.model}`"
                ) from exc
            raise ProviderError(f"Ollama error {exc.response.status_code}: {body}") from exc
        data = resp.json()
        return (data.get("message") or {}).get("content") or ""

    def health(self) -> str:
        try:
            resp = self._client.get(f"{self.host}/api/tags", timeout=5.0)
            resp.raise_for_status()
        except Exception as exc:
            raise ProviderError(
                f"Cannot reach Ollama at {self.host}. Install it (`brew install ollama`) "
                "and run `ollama serve`."
            ) from exc
        names = [m.get("name", "") for m in resp.json().get("models", [])]
        base = self.model.split(":")[0]
        if not any(n == self.model or n.split(":")[0] == base for n in names):
            raise ProviderError(
                f"Model '{self.model}' is not pulled. Run: ollama pull {self.model}\n"
                f"      Available: {', '.join(names) or '(none)'}"
            )
        return f"{self.host} — {self.model} (ctx {self.num_ctx})"
