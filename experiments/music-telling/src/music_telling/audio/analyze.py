"""Combine preview download, librosa features and Essentia moods."""

from __future__ import annotations

from ..cache import cached_json
from ..config import settings
from ..models import AudioFeatures, Track
from . import download


def analyze_track(track: Track) -> AudioFeatures | None:
    """Full audio analysis for one track, cached by track identity."""
    if not settings.use_audio:
        return None

    ident = f"{track.deezer_id or track.slug}|v1|mood={settings.use_mood}"

    def _run() -> dict | None:
        path = download.preview_path(track)
        if path is None:
            return None
        from . import features as feat

        data = feat.extract(path)
        if not data:
            return None
        if settings.use_mood:
            from . import mood

            data["moods"] = mood.predict(path)
        return data

    raw = cached_json("audio-features", ident, _run)
    return AudioFeatures(**raw) if raw else None
