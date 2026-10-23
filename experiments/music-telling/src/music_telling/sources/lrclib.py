"""Lyrics from LRCLIB - an open, key-free lyrics database."""

from __future__ import annotations

from ..cache import cached_json
from ..models import Lyrics, Track
from .http import get_json

API = "https://lrclib.net/api"


def _pick(rows: list[dict], track: Track) -> dict | None:
    """Prefer an entry with real lyrics whose duration matches the recording."""
    usable = [r for r in rows if r.get("plainLyrics") or r.get("instrumental")]
    if not usable:
        return None
    if track.duration:
        usable.sort(key=lambda r: abs((r.get("duration") or 0) - track.duration))
        if abs((usable[0].get("duration") or 0) - track.duration) <= 8:
            return usable[0]
    # Fall back to the first entry that actually carries text.
    for r in usable:
        if r.get("plainLyrics"):
            return r
    return usable[0]


def fetch_lyrics(track: Track) -> Lyrics | None:
    ident = f"{track.artist}|{track.title}"
    rows = cached_json(
        "lrclib",
        ident,
        lambda: get_json(
            f"{API}/search",
            {"track_name": track.title, "artist_name": track.artist},
        ),
    )
    if not rows:
        return None
    row = _pick(rows if isinstance(rows, list) else [], track)
    if not row:
        return None
    if row.get("instrumental"):
        return Lyrics(text="", instrumental=True)
    text = (row.get("plainLyrics") or "").strip()
    return Lyrics(text=text) if text else None
