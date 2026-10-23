"""Shared HTTP client with polite defaults and retry."""

from __future__ import annotations

import httpx2 as httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import USER_AGENT

_client: httpx.Client | None = None


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
        )
    return _client


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    reraise=True,
)
def get_json(url: str, params: dict | None = None) -> dict:
    resp = client().get(url, params=params)
    resp.raise_for_status()
    return resp.json()
