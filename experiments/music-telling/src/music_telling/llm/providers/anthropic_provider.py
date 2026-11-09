"""Anthropic backend - optional, used when a key is configured."""

from __future__ import annotations

from .base import ProviderError


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str):
        self.model = model
        self._client = None

    def _get(self):
        if self._client is None:
            try:
                import anthropic

                self._client = anthropic.Anthropic()
            except Exception as exc:
                raise ProviderError(
                    "No Anthropic credentials. Set ANTHROPIC_API_KEY or run `ant auth login`."
                ) from exc
        return self._client

    def complete(self, *, system: list[str], user: str, schema: dict, max_tokens: int) -> str:
        # Mark the last block cacheable: the theme brief is identical across
        # every per-song judging call.
        blocks = [{"type": "text", "text": b} for b in system]
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
        resp = self._get().messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=blocks,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        return "".join(b.text for b in resp.content if b.type == "text")

    def health(self) -> str:
        self._get().models.retrieve(self.model)
        return f"anthropic — {self.model}"
