"""Ranking arithmetic and lyric-selection rules."""

from music_telling.models import (
    EnrichedTrack,
    Judgement,
    ScoredTrack,
    Track,
)
from music_telling.sources.lrclib import _pick


def _scored(alignment, distinct):
    return ScoredTrack(
        enriched=EnrichedTrack(track=Track(title="T", artist="A")),
        judgement=Judgement(
            theme_alignment=alignment,
            evidence=[],
            narrative_role="r",
            distinctness=distinct,
            justification="j",
            keep=True,
        ),
    )


def test_alignment_dominates_distinctness():
    # A maximally distinct but less on-theme song must not outrank a strong match.
    assert _scored(90, 0).score > _scored(70, 100).score


def test_distinctness_bonus_is_bounded():
    """Distinctness can never move a song more than the fixed bonus.

    So it settles near-ties (where alignment differences are noise) but cannot
    overturn a gap wider than the bonus.
    """
    bonus = ScoredTrack.DISTINCTNESS_BONUS
    assert _scored(80, 100).score - _scored(80, 0).score == bonus
    gap = int(bonus) + 1
    assert _scored(75 + gap, 0).score > _scored(75, 100).score


def test_distinctness_breaks_ties():
    assert _scored(80, 90).score > _scored(80, 10).score


def test_pick_prefers_matching_duration():
    track = Track(title="T", artist="A", duration=296)
    rows = [
        {"plainLyrics": "wrong", "duration": 180},
        {"plainLyrics": "right", "duration": 297},
    ]
    assert _pick(rows, track)["plainLyrics"] == "right"


def test_pick_skips_entries_without_text():
    track = Track(title="T", artist="A")
    rows = [{"plainLyrics": "", "duration": 10}, {"plainLyrics": "real", "duration": 20}]
    assert _pick(rows, track)["plainLyrics"] == "real"


def test_pick_returns_none_when_nothing_usable():
    assert _pick([{"plainLyrics": ""}], Track(title="T", artist="A")) is None
