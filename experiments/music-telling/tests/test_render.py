"""Markdown rendering, including the beat->track association."""

from music_telling.models import (
    AudioFeatures,
    EnrichedTrack,
    Judgement,
    ScoredTrack,
    Story,
    StoryBeat,
    ThemeBrief,
    Track,
)
from music_telling.pipeline.run import PipelineResult
from music_telling.render import to_markdown


def _result():
    def scored(title, bpm):
        return ScoredTrack(
            enriched=EnrichedTrack(
                track=Track(title=title, artist="A", link=f"http://x/{title}"),
                features=AudioFeatures(tempo=bpm, key="C", mode="major"),
            ),
            judgement=Judgement(
                theme_alignment=80,
                evidence=[],
                narrative_role="r",
                distinctness=50,
                justification="j",
                keep=True,
            ),
        )

    ordered = [scored("Levels (Radio Edit)", 123.0), scored("Hurt", 92.0)]
    story = Story(
        title="T",
        logline="L",
        # The model normalized the first title - matching must not rely on it.
        beats=[
            StoryBeat(
                position=1,
                title="Levels",
                artist="A",
                beat_name="b",
                prose="p1",
                quote="q1",
                transition="t1",
            ),
            StoryBeat(
                position=2,
                title="Hurt",
                artist="A",
                beat_name="b",
                prose="p2",
                quote="",
                transition="",
            ),
        ],
        coda="C",
    )
    return PipelineResult(
        theme="th",
        brief=ThemeBrief(
            restatement="r",
            emotional_arc="a",
            motifs=["m1", "m2", "m3"],
            search_queries=["songs about a", "songs about b"],
            anti_motifs=["x"],
        ),
        story=story,
        ordered=ordered,
        all_scored=ordered,
        stats={"proposed": 5, "resolved": 2},
        eliminated=["X - Y (cut)"],
    )


def test_beats_match_by_position_not_title():
    md = to_markdown(_result())
    # Beat 1's audio must render even though its title was normalized.
    assert "123 BPM" in md and "92 BPM" in md


def test_quote_rendered_and_empty_quote_skipped():
    md = to_markdown(_result())
    assert "> q1" in md
    assert md.count("\n> ") == 1


def test_provenance_section_present():
    md = to_markdown(_result())
    assert "How this story was selected" in md
    assert "X - Y (cut)" in md
