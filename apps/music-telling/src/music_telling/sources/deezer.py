"""Deezer catalog lookup.

Deezer's public API needs no key and returns a 30-second preview MP3 per track,
which is what makes real audio analysis possible without a licensing deal.
"""

from __future__ import annotations

import re
import unicodedata

from ..cache import cached_json
from ..models import Candidate, Track
from .http import get_json

API = "https://api.deezer.com"


def _norm(s: str) -> str:
    """Fold case, accents and punctuation so titles compare sanely."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    # Drop parenthetical qualifiers: "(Remastered 2011)", "- Live", "feat. X"
    s = re.sub(r"\((?:feat|ft|with)[^)]*\)", " ", s)
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"\s-\s.*$", " ", s)
    s = re.sub(r"\bfeat\.?\b.*$", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _score_match(cand: Candidate, row: dict) -> float:
    """How well a Deezer row matches what we asked for."""
    want_t, want_a = _norm(cand.title), _norm(cand.artist)
    got_t, got_a = _norm(row.get("title", "")), _norm(row.get("artist", {}).get("name", ""))
    if not got_t or not got_a:
        return 0.0
    title = 1.0 if want_t == got_t else (0.6 if want_t in got_t or got_t in want_t else 0.0)
    artist = 1.0 if want_a == got_a else (0.6 if want_a in got_a or got_a in want_a else 0.0)
    if title == 0.0 or artist == 0.0:
        return 0.0
    # Nudge toward the canonical (most popular) recording of a matched song.
    popularity = min(row.get("rank") or 0, 1_000_000) / 1_000_000
    return title * 2 + artist * 2 + popularity


def search_tracks(query: str, limit: int = 10) -> list[dict]:
    data = cached_json(
        "deezer-search",
        f"{query}|{limit}",
        lambda: get_json(f"{API}/search", {"q": query, "limit": limit}),
    )
    return (data or {}).get("data", []) or []


def resolve_track(cand: Candidate) -> Track | None:
    """Find the best catalog entry for a proposed song, or None if it isn't there."""
    queries = [
        f'track:"{cand.title}" artist:"{cand.artist}"',
        f"{cand.title} {cand.artist}",
    ]
    best: tuple[float, dict] | None = None
    for q in queries:
        for row in search_tracks(q, limit=10):
            s = _score_match(cand, row)
            if s > 0 and (best is None or s > best[0]):
                best = (s, row)
        if best and best[0] >= 4.0:  # exact title + exact artist; stop looking
            break
    if not best:
        return None
    row = best[1]
    return Track(
        title=row.get("title") or cand.title,
        artist=(row.get("artist") or {}).get("name") or cand.artist,
        deezer_id=row.get("id"),
        album=(row.get("album") or {}).get("title"),
        isrc=row.get("isrc"),
        preview_url=row.get("preview") or None,
        duration=row.get("duration"),
        rank=row.get("rank"),
        link=row.get("link"),
    )
