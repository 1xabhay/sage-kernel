"""Fetch and cache the 30-second preview MP3 for a track."""

from __future__ import annotations

from pathlib import Path

from ..cache import cache_path
from ..models import Track
from ..sources.http import client


def preview_path(track: Track) -> Path | None:
    """Download the preview if needed; return the local path, or None."""
    if not track.preview_url:
        return None
    ident = str(track.deezer_id or track.preview_url)
    path = cache_path("audio", ident, ".mp3")
    if path.exists() and path.stat().st_size > 10_000:
        return path
    try:
        resp = client().get(track.preview_url)
        resp.raise_for_status()
        data = resp.content
    except Exception:
        return None
    if len(data) < 10_000:  # truncated or an error page, not audio
        return None
    path.write_bytes(data)
    return path
