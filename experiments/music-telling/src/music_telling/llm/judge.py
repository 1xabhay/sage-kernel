"""Stages 4a-4c: screen, score, then select and sequence."""

from __future__ import annotations

from ..config import settings
from ..models import (
    Candidate,
    EnrichedTrack,
    Judgement,
    ScoredTrack,
    ScreenVerdict,
    StoryOrder,
    ThemeBrief,
)
from .client import cached_system, structured

_SCREEN_SYSTEM = """You are triaging a candidate pool before expensive analysis.

Keep a candidate only if it plausibly embodies the theme in its lyrics. Cut:
- duplicates and near-duplicates (same song, different recording)
- songs matching only an anti-motif
- songs whose connection is the title alone
- novelty or joke entries that would break a serious narrative

Keep breadth: when two songs fill the same slot in the arc, keep the stronger \
one. You are cutting to a shortlist, so be decisive."""

_JUDGE_SYSTEM = """You are scoring one song's fit to a theme, for a narrative playlist.

You are given the song's lyrics and audio analysis computed from the recording \
(tempo, key, energy, and mood-classifier probabilities from a music model).

Scoring discipline:
- theme_alignment measures how directly the LYRICS embody the theme. Audio mood \
  supports or undercuts the reading; it never substitutes for lyrical evidence.
- Quote evidence VERBATIM from the lyrics provided. Never paraphrase into the \
  quote field, and never invent a line. If no lyrics were provided, return an \
  empty evidence list and cap theme_alignment at 55, since you cannot verify it.
- A famous song that merely gestures at the theme scores lower than an obscure \
  one that is unmistakably about it.
- distinctness penalizes interchangeability, not obscurity.
- narrative_role should name a specific position in an arc, not restate the theme."""

_ORDER_SYSTEM = """You are the final editor choosing and sequencing a story.

You receive scored finalists. Choose exactly the requested number and order them \
so the result reads as ONE story with a beginning, a turn, and an end.

Judge the sequence, not just the songs:
- The arc must move. Two adjacent songs occupying the same emotional position is \
  a defect, even if both score highly.
- Drop a high-scoring song when it is redundant with a stronger neighbour, and \
  say so in the rationale.
- Use the audio analysis for pacing: energy and tempo should shape the arc, not \
  fight it.
- The opener must establish the theme's world; the closer must land it."""


def _theme_block(theme: str, brief: ThemeBrief) -> str:
    return f"""THEME: {theme}
Restatement: {brief.restatement}
Emotional arc: {brief.emotional_arc}
Motifs that signal the theme: {", ".join(brief.motifs)}
Anti-motifs to eliminate: {", ".join(brief.anti_motifs)}"""


def screen(theme: str, brief: ThemeBrief, candidates: list[Candidate], keep: int) -> ScreenVerdict:
    listing = "\n".join(
        f"{i}. {c.title} - {c.artist} :: {c.reason}" for i, c in enumerate(candidates)
    )
    user = f"""{_theme_block(theme, brief)}

CANDIDATES
{listing}

Keep at most {keep}. Return the indices worth full analysis."""
    return structured(schema=ScreenVerdict, system=_SCREEN_SYSTEM, user=user, max_tokens=6000)


def judge_one(theme: str, brief: ThemeBrief, enriched: EnrichedTrack) -> ScoredTrack:
    """Score a single enriched track. The theme block is cached across calls."""
    judgement = structured(
        schema=Judgement,
        system=cached_system(_JUDGE_SYSTEM, _theme_block(theme, brief)),
        user=f"Score this song.\n\n{enriched.brief(settings.lyric_chars)}",
        max_tokens=4000,
        effort="medium",
    )
    return ScoredTrack(enriched=enriched, judgement=judgement)


def select_and_order(
    theme: str, brief: ThemeBrief, scored: list[ScoredTrack], length: int
) -> StoryOrder:
    listing = []
    for i, s in enumerate(scored):
        f = s.enriched.features
        listing.append(
            f"{i}. {s.enriched.track.title} - {s.enriched.track.artist}\n"
            f"   alignment={s.judgement.theme_alignment} distinct={s.judgement.distinctness}\n"
            f"   role: {s.judgement.narrative_role}\n"
            f"   audio: {f.summary() if f else 'n/a'}\n"
            f"   why: {s.judgement.justification}"
        )
    user = f"""{_theme_block(theme, brief)}

FINALISTS
{chr(10).join(listing)}

Choose exactly {length} and order them into one coherent story."""
    return structured(schema=StoryOrder, system=_ORDER_SYSTEM, user=user, max_tokens=8000)
