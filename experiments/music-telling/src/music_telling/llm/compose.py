"""Stage 4d: write the story that the chosen sequence tells."""

from __future__ import annotations

from ..config import settings
from ..models import ScoredTrack, Story, ThemeBrief
from .client import structured

_COMPOSE_SYSTEM = """You are a music writer composing a single continuous story \
told through an ordered sequence of songs.

This is narrative prose, not a playlist description. Requirements:
- The beats must read as ONE story. Each beat inherits the situation the previous \
  beat left behind; the `transition` line makes that handoff explicit.
- Ground every beat in the song's ACTUAL lyrics. The `quote` field must be a \
  verbatim line from the lyrics you were given - never invented, never \
  paraphrased. If a song's lyrics were unavailable, use an empty quote and write \
  the beat from its audio character instead.
- Use the audio analysis for texture: a 78 BPM acoustic track in a minor key and \
  a 140 BPM aggressive one earn different sentences.
- Do not summarize the song or review it. Advance the story.
- Never state the theme outright in the prose. The story should demonstrate it.
- The coda lands the theme without moralizing."""


def compose(theme: str, brief: ThemeBrief, ordered: list[ScoredTrack]) -> Story:
    blocks = []
    for i, s in enumerate(ordered, start=1):
        t = s.enriched.track
        f = s.enriched.features
        lyrics = (
            s.enriched.lyrics.excerpt(settings.lyric_chars)
            if s.enriched.lyrics and not s.enriched.lyrics.instrumental
            else "(lyrics unavailable)"
        )
        blocks.append(
            f"""--- POSITION {i} ---
TITLE: {t.title}
ARTIST: {t.artist}
NARRATIVE ROLE: {s.judgement.narrative_role}
AUDIO: {f.summary() if f else "not analyzed"}
LYRICS:
{lyrics}"""
        )
    user = f"""Theme: {theme}
The arc this story should travel: {brief.emotional_arc}

Write the story across these {len(ordered)} songs, in this exact order.
Produce exactly one beat per song, with `position` matching the POSITION number.

{chr(10).join(blocks)}"""
    return structured(
        schema=Story, system=_COMPOSE_SYSTEM, user=user, max_tokens=16000, effort="high"
    )
