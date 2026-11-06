"""Signal features from the preview, via librosa.

These replace the retired Spotify audio-features endpoint with numbers we
compute ourselves, so the pipeline has no proprietary dependency.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

# Krumhansl-Schmuckler key profiles, used to estimate key and mode from chroma.
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
_PITCHES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _estimate_key(chroma_mean: np.ndarray) -> tuple[str, str]:
    """Correlate the average chroma against rotated major/minor profiles."""
    best = (-2.0, "C", "major")
    for i in range(12):
        for profile, name in ((_MAJOR, "major"), (_MINOR, "minor")):
            rotated = np.roll(profile, i)
            num = np.corrcoef(chroma_mean, rotated)[0, 1]
            if np.isfinite(num) and num > best[0]:
                best = (float(num), _PITCHES[i], name)
    return best[1], best[2]


def extract(path: Path) -> dict:
    """Return the librosa-derived portion of AudioFeatures as a dict."""
    import librosa

    y, sr = librosa.load(str(path), sr=22050, mono=True)
    if y.size == 0:
        return {}

    tempo = float(np.atleast_1d(librosa.feature.tempo(y=y, sr=sr))[0])
    rms = librosa.feature.rms(y=y)[0]
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr)
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    key, mode = _estimate_key(chroma.mean(axis=1))

    duration = len(y) / sr
    return {
        "tempo": round(tempo, 1),
        "key": key,
        "mode": mode,
        # RMS is already ~0-1 for normalized audio; clamp so it reads as a ratio.
        "energy": round(float(np.clip(rms.mean(), 0, 1)), 4),
        "brightness": round(float(centroid.mean()), 1),
        "dynamics": round(float(rms.std()), 4),
        "onset_rate": round(len(onsets) / duration, 2) if duration else None,
    }
