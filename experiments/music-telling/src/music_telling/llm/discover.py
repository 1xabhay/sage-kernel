"""Stage 1-2: read the theme, then propose songs grounded in web results."""

from __future__ import annotations

from pydantic import Field, create_model

from ..config import settings
from ..models import Candidate, ThemeBrief
from ..sources.websearch import gather_context
from .client import structured

_BRIEF_SYSTEM = """You are a music supervisor planning a narrative playlist.

Given a theme, you produce a working brief: what the theme actually means, the \
emotional arc a story about it should travel, the concrete lyrical motifs that \
signal it in a song, and the adjacent themes that are easy to confuse with it.

Motifs must be concrete and checkable against lyrics (images, situations, \
actions) - not abstractions. "Packing a suitcase at night" is a motif; \
"feelings of change" is not.

Anti-motifs matter as much: name the themes that will flood a naive search and \
should be eliminated."""

_DISCOVER_SYSTEM = """You are a music supervisor assembling a candidate pool.

Propose songs that genuinely embody the theme in their LYRICS, not merely in \
mood or title. Rules:
- Real, released, findable recordings. No invented songs.
- Give the primary performing artist, not a cover act, unless the cover is the \
famous one.
- Span eras, genres and perspectives; a pool of one genre makes a flat story.
- Cover every stage of the emotional arc in the brief, not just its peak.
- Exclude songs that only match an anti-motif.
- Prefer songs where a specific lyric would survive being quoted as evidence.

The web results provided are human-curated songlists. Use them to go beyond \
what you recall, but never copy a song you cannot vouch for."""


def build_brief(theme: str) -> ThemeBrief:
    return structured(
        schema=ThemeBrief,
        system=_BRIEF_SYSTEM,
        user=f"Theme: {theme}",
        max_tokens=4000,
    )


def normalize_queries(theme: str, queries: list[str]) -> list[str]:
    """Make sure every query actually searches for songs about the theme.

    Smaller models sometimes fill this field with bare concepts (or even the
    anti-motifs), which would ground discovery on the wrong material.
    """
    out: list[str] = []
    for q in queries:
        q = " ".join((q or "").split())
        if not q:
            continue
        if "song" not in q.lower() and "track" not in q.lower():
            q = f"songs about {q}"
        out.append(q)
    # Always include a query derived straight from the theme as a floor.
    baseline = f"songs about {theme}"
    if baseline.lower() not in {o.lower() for o in out}:
        out.insert(0, baseline)
    seen: set[str] = set()
    return [q for q in out if not (q.lower() in seen or seen.add(q.lower()))]


def propose_candidates(theme: str, brief: ThemeBrief, target: int) -> list:
    """Propose ~`target` candidate songs, grounded with web search results."""
    # Leave room for the instructions and the model's own output.
    context = gather_context(
        normalize_queries(theme, brief.search_queries),
        per_query=6,
        max_chars=settings.web_context_chars,
    )
    user = f"""Theme: {theme}

THEME BRIEF
Restatement: {brief.restatement}
Emotional arc: {brief.emotional_arc}
Motifs: {", ".join(brief.motifs)}
Avoid (anti-motifs): {", ".join(brief.anti_motifs)}

WEB SEARCH RESULTS
{context or "(no web results available - rely on your own knowledge)"}

Propose exactly {target} candidate songs covering the full arc."""
    # Bound the array to what we actually asked for. A loose bound lets a small
    # model keep emitting items long after it has run out of real songs.
    bounded = create_model(
        "BoundedCandidateList",
        candidates=(list[Candidate], Field(min_length=1, max_length=target)),
    )
    result = structured(
        schema=bounded,
        system=_DISCOVER_SYSTEM,
        user=user,
        max_tokens=max(2000, target * 120),
    )
    return result.candidates
