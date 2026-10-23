"""Audio feature helpers that don't need a network or a model."""

import numpy as np

from music_telling.audio.features import _MAJOR, _MINOR, _estimate_key
from music_telling.models import AudioFeatures, EnrichedTrack, Lyrics, Track


def test_estimate_key_recovers_profile():
    # A pure C-major profile should be read back as C major.
    key, mode = _estimate_key(_MAJOR.copy())
    assert (key, mode) == ("C", "major")
    # Rotating the minor profile by 9 semitones gives A minor.
    key, mode = _estimate_key(np.roll(_MINOR, 9))
    assert (key, mode) == ("A", "minor")


def test_features_summary_is_readable():
    f = AudioFeatures(
        tempo=103.0,
        key="A",
        mode="major",
        energy=0.07,
        brightness=2206.0,
        dynamics=0.02,
        moods={"sad": 0.98, "acoustic": 0.99, "party": 0.01},
    )
    s = f.summary()
    assert "103 BPM" in s and "A major" in s
    # Confident moods are surfaced; near-zero ones are suppressed as noise.
    assert "acoustic 0.99" in s and "sad 0.98" in s
    assert "party" not in s


def test_summary_omits_moods_when_none_are_confident():
    f = AudioFeatures(tempo=90.0, moods={"sad": 0.1, "party": 0.2})
    assert "mood" not in f.summary()


def test_empty_features_summary():
    assert AudioFeatures().summary() == "no audio analysis"


def test_brief_marks_missing_lyrics():
    t = Track(title="X", artist="Y")
    assert "unavailable or instrumental" in EnrichedTrack(track=t).brief()
    e = EnrichedTrack(track=t, lyrics=Lyrics(text="a line"))
    assert "a line" in e.brief()


def test_lyrics_excerpt_truncates():
    ly = Lyrics(text="x" * 5000)
    assert ly.excerpt(100).endswith("[...]") and len(ly.excerpt(100)) < 200
