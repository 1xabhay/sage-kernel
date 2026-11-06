"""Provider protocol and the JSON-schema plumbing every backend shares."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError


class ProviderError(RuntimeError):
    """Backend is unreachable or misconfigured."""


class Provider(Protocol):
    name: str

    def complete(self, *, system: list[str], user: str, schema: dict, max_tokens: int) -> str:
        """Return a JSON string conforming to `schema`."""
        ...

    def health(self) -> str:
        """Human-readable status, or raise ProviderError."""
        ...


def json_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema with all $refs inlined.

    Local constrained-decoding backends (llama.cpp's GBNF, which Ollama uses)
    handle `$defs`/`$ref` inconsistently, so nested models like Story->StoryBeat
    must be flattened before they are sent.
    """
    return _seal(_inline_refs(model.model_json_schema()))


def _inline_refs(schema: dict) -> dict:
    defs = schema.pop("$defs", {})

    def walk(node: Any, depth: int = 0) -> Any:
        if depth > 12:  # cyclic model; leave as-is rather than recurse forever
            return node
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = defs.get(ref.split("/")[-1])
                if target is not None:
                    merged = {**walk(target, depth + 1)}
                    # Preserve sibling keys such as `description`.
                    for k, v in node.items():
                        if k != "$ref":
                            merged[k] = walk(v, depth + 1)
                    return merged
            return {k: walk(v, depth + 1) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, depth + 1) for v in node]
        return node

    return walk(schema)


def _seal(node: Any) -> Any:
    """Set additionalProperties=false on every object.

    Anthropic's json_schema output format requires it for strict validation, and
    it stops local models from inventing extra keys during constrained decoding.
    """
    if isinstance(node, dict):
        out = {k: _seal(v) for k, v in node.items()}
        if "properties" in out and out.get("type", "object") == "object":
            out.setdefault("additionalProperties", False)
        return out
    if isinstance(node, list):
        return [_seal(v) for v in node]
    return node


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> str:
    """Pull the JSON object out of a model response.

    Smaller models wrap output in prose or code fences even when asked not to,
    and reasoning models emit <think> blocks first.
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    if text.startswith("{"):
        return text
    start = text.find("{")
    if start == -1:
        return text
    # Walk to the matching brace so trailing commentary is discarded.
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


def parse_into[T: BaseModel](schema: type[T], raw: str) -> T:
    return schema.model_validate_json(extract_json(raw))


def validation_hint(exc: ValidationError) -> str:
    """A short, model-readable summary of what was wrong."""
    lines = []
    for err in exc.errors()[:6]:
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"- {loc}: {err['msg']}")
    return "\n".join(lines)


def dumps_compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
