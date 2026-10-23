"""Content-addressed disk cache.

Every network call and every audio analysis is cached, so re-running a theme is
cheap and the pipeline is reproducible while you iterate on prompts.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from .config import settings

T = TypeVar("T")


def _key(namespace: str, ident: str) -> Path:
    digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:32]
    d = settings.http_dir / namespace
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{digest}.json"


def cached_json(namespace: str, ident: str, produce: Callable[[], Any]) -> Any:
    """Return cached JSON for `ident`, else call `produce()` and store it.

    A cached `null` is treated as a real result (many lookups legitimately miss),
    so a failed lookup is not retried on every run.
    """
    path = _key(namespace, ident)
    if path.exists():
        try:
            return json.loads(path.read_text())["value"]
        except (json.JSONDecodeError, KeyError):
            path.unlink(missing_ok=True)
    value = produce()
    path.write_text(json.dumps({"ident": ident, "value": value}, ensure_ascii=False))
    return value


def cache_path(namespace: str, ident: str, suffix: str) -> Path:
    """A stable path for binary artifacts (audio, models)."""
    digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:32]
    d = settings.cache_dir / namespace
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{digest}{suffix}"
