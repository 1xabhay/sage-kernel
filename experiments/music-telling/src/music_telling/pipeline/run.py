"""The end-to-end pipeline: theme -> songs -> enrichment -> elimination -> story."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..audio import analyze_track
from ..config import settings
from ..llm import compose as compose_mod
from ..llm import discover as discover_mod
from ..llm import judge as judge_mod
from ..models import Candidate, EnrichedTrack, ScoredTrack, Story, ThemeBrief, Track
from ..sources.deezer import resolve_track
from ..sources.lrclib import fetch_lyrics

Progress = Callable[[str, str], None]


def _noop(stage: str, message: str) -> None:  # pragma: no cover
    pass


@dataclass
class PipelineResult:
    theme: str
    brief: ThemeBrief
    story: Story
    ordered: list[ScoredTrack]
    all_scored: list[ScoredTrack]
    arc_rationale: str = ""
    eliminated: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _enrich(cand_track: tuple[Candidate, Track]) -> EnrichedTrack:
    cand, track = cand_track
    lyrics = fetch_lyrics(track)
    features = analyze_track(track)
    return EnrichedTrack(track=track, lyrics=lyrics, features=features, reason=cand.reason)


def run_pipeline(
    theme: str,
    *,
    story_length: int | None = None,
    pool_size: int | None = None,
    progress: Progress = _noop,
) -> PipelineResult:
    length = story_length or settings.story_length
    pool = pool_size or settings.candidate_pool
    stats: dict = {}
    eliminated: list[str] = []

    # --- 1. Understand the theme -------------------------------------------
    progress("theme", "reading the theme")
    brief = discover_mod.build_brief(theme)
    progress("theme", f"arc: {brief.emotional_arc}")

    # --- 2. Discover candidates (LLM grounded by web search) ----------------
    progress("discover", f"searching the web and proposing ~{pool} songs")
    candidates = discover_mod.propose_candidates(theme, brief, pool)
    stats["proposed"] = len(candidates)
    progress("discover", f"{len(candidates)} candidates proposed")

    # --- 3a. Cheap screen before any network/audio spend --------------------
    shortlist = candidates
    if len(candidates) > settings.shortlist_size:
        progress("screen", f"screening {len(candidates)} down to {settings.shortlist_size}")
        verdict = judge_mod.screen(theme, brief, candidates, settings.shortlist_size)
        valid = [i for i in verdict.keep_indices if 0 <= i < len(candidates)]
        if valid:
            kept = set(valid)
            eliminated += [
                f"{c.title} - {c.artist} (screened out)"
                for i, c in enumerate(candidates)
                if i not in kept
            ]
            shortlist = [candidates[i] for i in valid]
        progress("screen", verdict.cut_rationale)
    stats["shortlisted"] = len(shortlist)

    # --- 3b. Resolve against the catalog -----------------------------------
    progress("resolve", f"resolving {len(shortlist)} songs on Deezer")
    with ThreadPoolExecutor(max_workers=settings.max_workers) as ex:
        resolved = list(ex.map(resolve_track, shortlist))
    pairs = [(c, t) for c, t in zip(shortlist, resolved, strict=True) if t is not None]
    eliminated += [
        f"{c.title} - {c.artist} (not found in catalog)"
        for c, t in zip(shortlist, resolved, strict=True)
        if t is None
    ]
    stats["resolved"] = len(pairs)
    progress("resolve", f"{len(pairs)} resolved, {len(shortlist) - len(pairs)} not found")

    if not pairs:
        raise RuntimeError("No candidate songs could be resolved in the catalog.")

    # --- 3c. Lyrics + audio features ---------------------------------------
    progress("enrich", f"fetching lyrics and analyzing audio for {len(pairs)} songs")
    with ThreadPoolExecutor(max_workers=settings.max_workers) as ex:
        enriched = list(ex.map(_enrich, pairs))
    with_lyrics = sum(1 for e in enriched if e.lyrics and e.lyrics.text)
    with_audio = sum(1 for e in enriched if e.features)
    stats["with_lyrics"] = with_lyrics
    stats["with_audio"] = with_audio
    progress("enrich", f"lyrics for {with_lyrics}, audio analysis for {with_audio}")

    # --- 4a. Score every survivor ------------------------------------------
    progress("rank", f"scoring {len(enriched)} songs against the theme")

    def _judge(e: EnrichedTrack) -> ScoredTrack | None:
        try:
            return judge_mod.judge_one(theme, brief, e)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=settings.llm_workers) as ex:
        scored_raw = list(ex.map(_judge, enriched))
    all_scored = sorted((s for s in scored_raw if s is not None), key=lambda s: -s.score)
    if not all_scored:
        raise RuntimeError("Scoring failed for every candidate.")

    # --- 4b. Elimination round ---------------------------------------------
    survivors = [
        s
        for s in all_scored
        if s.judgement.keep and s.judgement.theme_alignment >= settings.screen_threshold
    ]
    eliminated += [
        f"{s.enriched.track.slug} (alignment {s.judgement.theme_alignment})"
        for s in all_scored
        if s not in survivors
    ]
    # Never eliminate below what the story needs; fall back to the best scorers.
    if len(survivors) < length:
        survivors = all_scored[: max(length, len(survivors))]
    progress("rank", f"{len(survivors)} survived elimination")
    stats["survivors"] = len(survivors)

    # --- 4c. Final selection and sequencing --------------------------------
    if len(survivors) > length:
        progress("order", f"selecting and sequencing {length} of {len(survivors)}")
        order = judge_mod.select_and_order(theme, brief, survivors, length)
        idx = [i for i in order.chosen if 0 <= i < len(survivors)][:length]
        seen: set[int] = set()
        idx = [i for i in idx if not (i in seen or seen.add(i))]
        ordered = [survivors[i] for i in idx]
        # Backfill if the model returned too few valid indices.
        for s in survivors:
            if len(ordered) >= length:
                break
            if s not in ordered:
                ordered.append(s)
        rationale = order.arc_rationale
        eliminated += [
            f"{s.enriched.track.slug} (cut in final edit)" for s in survivors if s not in ordered
        ]
    else:
        ordered = survivors[:length]
        rationale = "Every survivor was needed to reach the requested length."

    # --- 5. Compose ---------------------------------------------------------
    progress("compose", f"writing the story across {len(ordered)} songs")
    story = compose_mod.compose(theme, brief, ordered)
    progress("compose", "done")

    return PipelineResult(
        theme=theme,
        brief=brief,
        story=story,
        ordered=ordered,
        all_scored=all_scored,
        arc_rationale=rationale,
        eliminated=eliminated,
        stats=stats,
    )
