"""End-to-end orchestration with every network call and LLM stubbed."""

import pytest

from music_telling import models as M
from music_telling.llm import compose as compose_mod
from music_telling.llm import discover as discover_mod
from music_telling.llm import judge as judge_mod
from music_telling.pipeline import run as run_mod

CANDS = [("A", "aa"), ("B", "bb"), ("C", "cc"), ("D", "dd"), ("Ghost", "nobody")]


def _brief():
    return M.ThemeBrief(
        restatement="r",
        emotional_arc="a -> b",
        motifs=["m1", "m2", "m3"],
        search_queries=["songs about q1", "songs about q2"],
        anti_motifs=["x"],
    )


@pytest.fixture
def stubbed(monkeypatch):
    """Wire deterministic stand-ins for every external dependency."""
    state = {"screen_keep": None, "chosen": None}

    def fake_structured(*, schema, system, user, max_tokens=8000, effort="high"):
        if schema is M.ThemeBrief:
            return _brief()
        # Discovery builds a dynamically bounded model, so match on shape.
        if "candidates" in schema.model_fields:
            return schema(candidates=[M.Candidate(title=t, artist=a, reason="r") for t, a in CANDS])
        if schema is M.ScreenVerdict:
            keep = state["screen_keep"]
            return M.ScreenVerdict(
                keep_indices=list(range(len(CANDS))) if keep is None else keep,
                cut_rationale="c",
            )
        if schema is M.Judgement:
            # "D" is deliberately off-theme so elimination has something to do.
            off = "TITLE: D" in user
            return M.Judgement(
                theme_alignment=20 if off else 85,
                evidence=[],
                narrative_role="role",
                distinctness=50,
                justification="j",
                keep=not off,
            )
        if schema is M.StoryOrder:
            n = user.count("\n   role:")
            chosen = state["chosen"] if state["chosen"] is not None else list(range(n))
            return M.StoryOrder(chosen=chosen, arc_rationale="ar")
        if schema is M.Story:
            positions = user.count("--- POSITION")
            return M.Story(
                title="T",
                logline="L",
                beats=[
                    M.StoryBeat(
                        position=i + 1,
                        title="t",
                        artist="a",
                        beat_name="b",
                        prose="p",
                        quote="q",
                        transition="",
                    )
                    for i in range(positions)
                ],
                coda="C",
            )
        raise AssertionError(schema)

    for mod in (discover_mod, judge_mod, compose_mod):
        monkeypatch.setattr(mod, "structured", fake_structured)
    monkeypatch.setattr(discover_mod, "gather_context", lambda *a, **k: "")

    def fake_resolve(cand):
        if cand.artist == "nobody":  # simulates a hallucinated song
            return None
        return M.Track(title=cand.title, artist=cand.artist, deezer_id=hash(cand.title) % 999)

    monkeypatch.setattr(run_mod, "resolve_track", fake_resolve)
    monkeypatch.setattr(run_mod, "fetch_lyrics", lambda t: M.Lyrics(text=f"words for {t.title}"))
    monkeypatch.setattr(run_mod, "analyze_track", lambda t: M.AudioFeatures(tempo=100.0))
    return state


def test_unresolvable_songs_are_eliminated(stubbed):
    res = run_mod.run_pipeline("th", story_length=2, pool_size=5)
    assert res.stats["resolved"] == 4  # the hallucinated one is gone
    assert any("not found in catalog" in e for e in res.eliminated)


def test_low_alignment_song_is_eliminated(stubbed):
    res = run_mod.run_pipeline("th", story_length=2, pool_size=5)
    assert "D" not in [s.enriched.track.title for s in res.ordered]
    assert res.stats["survivors"] == 3


def test_story_length_is_respected(stubbed):
    res = run_mod.run_pipeline("th", story_length=2, pool_size=5)
    assert len(res.ordered) == 2
    assert len(res.story.beats) == 2


def test_backfills_when_editor_returns_too_few(stubbed):
    """A short/invalid `chosen` list must still yield a full-length story."""
    stubbed["chosen"] = [0]
    res = run_mod.run_pipeline("th", story_length=3, pool_size=5)
    assert len(res.ordered) == 3
    assert len({s.enriched.track.title for s in res.ordered}) == 3


def test_ignores_out_of_range_and_duplicate_indices(stubbed):
    stubbed["chosen"] = [0, 0, 99, -1, 1]
    res = run_mod.run_pipeline("th", story_length=2, pool_size=5)
    titles = [s.enriched.track.title for s in res.ordered]
    assert len(titles) == 2 and len(set(titles)) == 2


def test_raises_when_nothing_resolves(stubbed, monkeypatch):
    monkeypatch.setattr(run_mod, "resolve_track", lambda c: None)
    with pytest.raises(RuntimeError, match="resolved"):
        run_mod.run_pipeline("th", story_length=2, pool_size=5)


def test_llm_concurrency_is_throttled_for_local_models():
    """Ollama serializes per-model requests; a wide fan-out only adds pressure."""
    from music_telling.config import Settings

    local = Settings(provider="ollama")
    assert local.llm_workers == 2
    assert local.max_workers > local.llm_workers  # I/O stays parallel

    remote = Settings(provider="anthropic")
    assert remote.llm_workers == remote.max_workers
