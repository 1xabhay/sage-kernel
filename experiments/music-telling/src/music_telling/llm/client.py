"""Provider-agnostic structured completion.

Every LLM stage goes through `structured()`. Small local models drift from a
schema more often than frontier ones, so failures are retried with the
validation errors fed back rather than surfaced to the caller.
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError

from ..config import settings
from .providers import ProviderError, get_provider
from .providers.base import json_schema, parse_into, validation_hint

# Re-exported so callers can keep catching one error type.
LLMUnavailable = ProviderError

_JSON_RULE = (
    "Respond with a single JSON object matching the required schema. "
    "No prose, no code fences, no commentary before or after."
)


def structured[T: BaseModel](
    *,
    schema: type[T],
    system: str | list[str],
    user: str,
    max_tokens: int = 8000,
    effort: str = "high",  # honored by frontier providers; ignored locally
) -> T:
    """Return a validated `schema` instance from the configured provider."""
    blocks = [system] if isinstance(system, str) else list(system)
    blocks.append(_JSON_RULE)
    spec = json_schema(schema)
    provider = get_provider()

    attempt_user = user
    last_error: Exception | None = None

    for _attempt in range(settings.max_retries):
        try:
            raw = provider.complete(
                system=blocks, user=attempt_user, schema=spec, max_tokens=max_tokens
            )
        except ProviderError:
            raise
        except Exception as exc:  # transient backend hiccup
            last_error = exc
            continue

        try:
            return parse_into(schema, raw)
        except ValidationError as exc:
            last_error = exc
            # Show the model exactly which fields were wrong, then retry.
            attempt_user = (
                f"{user}\n\n"
                f"Your previous response failed validation:\n{validation_hint(exc)}\n"
                "Return corrected JSON for the same schema."
            )
        except ValueError as exc:  # not JSON at all
            last_error = exc
            attempt_user = (
                f"{user}\n\nYour previous response was not valid JSON. Return only the JSON object."
            )

    raise RuntimeError(
        f"{schema.__name__} could not be produced after {settings.max_retries} "
        f"attempts ({type(last_error).__name__}: {last_error})"
    )


def cached_system(static: str, volatile: str) -> list[str]:
    """System blocks; the Anthropic provider caches the stable prefix."""
    return [static, volatile]


def health() -> str:
    return get_provider().health()
