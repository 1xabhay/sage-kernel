"""Catalog matching and normalization - the logic most likely to silently rot."""

from music_telling.models import Candidate
from music_telling.sources.deezer import _norm, _score_match


def test_norm_strips_qualifiers():
    assert _norm("Fast Car (Remastered 2011)") == "fast car"
    assert _norm("Hurt - Live at Folsom") == "hurt"
    assert _norm("Stan feat. Dido") == "stan"
    assert _norm("Café Müller") == "cafe muller"


def _row(title, artist, rank=500_000):
    return {"title": title, "artist": {"name": artist}, "rank": rank}


def test_exact_match_beats_partial():
    cand = Candidate(title="Fast Car", artist="Tracy Chapman", reason="")
    exact = _score_match(cand, _row("Fast Car", "Tracy Chapman"))
    partial = _score_match(cand, _row("Fast Car (Live)", "Tracy Chapman Tribute"))
    assert exact > partial > 0


def test_wrong_artist_scores_zero():
    cand = Candidate(title="Fast Car", artist="Tracy Chapman", reason="")
    assert _score_match(cand, _row("Fast Car", "Jonas Blue")) == 0.0


def test_popularity_breaks_ties():
    cand = Candidate(title="Hurt", artist="Johnny Cash", reason="")
    popular = _score_match(cand, _row("Hurt", "Johnny Cash", rank=900_000))
    obscure = _score_match(cand, _row("Hurt", "Johnny Cash", rank=1_000))
    assert popular > obscure
