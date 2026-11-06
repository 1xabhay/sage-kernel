"""Render a PipelineResult as Markdown."""

from __future__ import annotations

from .pipeline.run import PipelineResult


def to_markdown(result: PipelineResult) -> str:
    s = result.story
    out: list[str] = [
        f"# {s.title}",
        "",
        f"*{s.logline}*",
        "",
        f"**Theme:** {result.theme}  ",
        f"**Arc:** {result.brief.emotional_arc}",
        "",
        "---",
        "",
    ]
    for beat in sorted(s.beats, key=lambda b: b.position):
        # Match by position - the composer is told to emit one beat per song with
        # `position` matching the input order, so this is exact. Titles are not:
        # the model may normalize "Levels (Radio Edit)" to "Levels".
        match = (
            result.ordered[beat.position - 1] if 1 <= beat.position <= len(result.ordered) else None
        )
        out.append(f"## {beat.position}. {beat.title} — {beat.artist}")
        out.append(f"*{beat.beat_name}*")
        out.append("")
        if beat.quote:
            out.append(f"> {beat.quote}")
            out.append("")
        out.append(beat.prose)
        out.append("")
        if match:
            f = match.enriched.features
            meta = [f"alignment **{match.judgement.theme_alignment}/100**"]
            if f:
                meta.append(f.summary())
            if match.enriched.track.link:
                meta.append(f"[listen]({match.enriched.track.link})")
            out.append("<sub>" + " · ".join(meta) + "</sub>")
            out.append("")
        if beat.transition:
            out.append(f"*{beat.transition}*")
            out.append("")
    out += ["---", "", "## Coda", "", s.coda, ""]

    out += ["---", "", "## How this story was selected", ""]
    out.append(f"- Proposed: **{result.stats.get('proposed', 0)}** candidates")
    out.append(f"- Shortlisted: **{result.stats.get('shortlisted', 0)}**")
    out.append(
        f"- Resolved in catalog: **{result.stats.get('resolved', 0)}** "
        f"(lyrics for {result.stats.get('with_lyrics', 0)}, "
        f"audio analysis for {result.stats.get('with_audio', 0)})"
    )
    out.append(f"- Survived elimination: **{result.stats.get('survivors', 0)}**")
    out.append(f"- Told: **{len(result.ordered)}**")
    if result.arc_rationale:
        out += ["", f"**Sequencing rationale:** {result.arc_rationale}"]
    if result.eliminated:
        out += ["", "<details><summary>Eliminated songs</summary>", ""]
        out += [f"- {e}" for e in result.eliminated]
        out += ["", "</details>"]
    return "\n".join(out)
