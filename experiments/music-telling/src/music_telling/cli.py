"""Command-line entry point."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import settings
from .llm.client import LLMUnavailable
from .pipeline import run_pipeline
from .render import to_markdown

app = typer.Typer(add_completion=False, help="Turn a theme into a story told through songs.")
console = Console()


@app.command()
def tell(
    theme: str = typer.Argument(..., help="The theme to build a story around."),
    length: int = typer.Option(settings.story_length, "--length", "-n", help="Songs in the story."),
    pool: int = typer.Option(
        settings.candidate_pool, "--pool", "-p", help="Candidates to propose."
    ),
    out: Path | None = typer.Option(None, "--out", "-o", help="Write Markdown here."),
    json_out: Path | None = typer.Option(None, "--json", help="Write the full result as JSON."),
    no_audio: bool = typer.Option(False, "--no-audio", help="Skip preview download and DSP."),
    no_mood: bool = typer.Option(False, "--no-mood", help="Skip Essentia mood models."),
    provider: str = typer.Option(
        settings.provider, "--provider", help="LLM backend: ollama (free, local) or anthropic."
    ),
    model: str | None = typer.Option(None, "--model", help="Model id for the provider."),
) -> None:
    """Build and print a song-told story for THEME."""
    settings.use_provider(provider, model)
    settings.use_audio = not no_audio
    settings.use_mood = not no_mood and not no_audio
    settings.ensure_dirs()

    with console.status("[bold]starting[/]") as status:

        def progress(stage: str, message: str) -> None:
            status.update(f"[bold cyan]{stage}[/] · {message}")
            console.log(f"[dim]{stage}[/] {message}")

        try:
            result = run_pipeline(theme, story_length=length, pool_size=pool, progress=progress)
        except LLMUnavailable as exc:
            console.print(f"[bold red]{exc}[/]")
            raise typer.Exit(code=2) from exc
        except RuntimeError as exc:
            console.print(f"[bold red]Pipeline failed:[/] {exc}")
            raise typer.Exit(code=1) from exc

    story = result.story
    console.print()
    console.print(Panel(f"[bold]{story.title}[/]\n[italic]{story.logline}[/]", expand=False))

    table = Table(title="The story", show_lines=False, header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("Song")
    table.add_column("Artist")
    table.add_column("Align", justify="right")
    table.add_column("Audio", overflow="fold")
    for i, s in enumerate(result.ordered, start=1):
        f = s.enriched.features
        table.add_row(
            str(i),
            s.enriched.track.title,
            s.enriched.track.artist,
            str(s.judgement.theme_alignment),
            f.summary() if f else "—",
        )
    console.print(table)

    markdown = to_markdown(result)
    if out:
        out.write_text(markdown)
        console.print(f"[green]Wrote[/] {out}")
    if json_out:
        payload = {
            "theme": result.theme,
            "brief": result.brief.model_dump(),
            "story": result.story.model_dump(),
            "ordered": [s.model_dump() for s in result.ordered],
            "all_scored": [s.model_dump() for s in result.all_scored],
            "eliminated": result.eliminated,
            "stats": result.stats,
            "arc_rationale": result.arc_rationale,
        }
        json_out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        console.print(f"[green]Wrote[/] {json_out}")
    if not out and not json_out:
        console.print()
        console.print(markdown)


@app.command()
def doctor(
    provider: str = typer.Option(settings.provider, "--provider", help="LLM backend to check."),
    model: str | None = typer.Option(None, "--model", help="Model id for the provider."),
) -> None:
    """Check that data sources, audio stack and the LLM backend are working."""
    settings.use_provider(provider, model)
    settings.ensure_dirs()
    ok = True

    from .models import Candidate
    from .sources.deezer import resolve_track
    from .sources.lrclib import fetch_lyrics

    probe = Candidate(title="Fast Car", artist="Tracy Chapman", reason="probe")
    try:
        track = resolve_track(probe)
        console.print(
            f"[green]OK[/] Deezer — resolved {track.slug}, preview={bool(track.preview_url)}"
        )
    except Exception as exc:
        ok = False
        track = None
        console.print(f"[red]FAIL[/] Deezer — {exc}")

    if track:
        try:
            ly = fetch_lyrics(track)
            console.print(f"[green]OK[/] LRCLIB — {len(ly.text) if ly else 0} chars of lyrics")
        except Exception as exc:
            ok = False
            console.print(f"[red]FAIL[/] LRCLIB — {exc}")

    try:
        from .sources.websearch import search_web

        hits = search_web("songs about leaving home", max_results=3)
        console.print(
            f"[green]OK[/] web search — {len(hits)} results"
            if hits
            else "[yellow]WARN[/] web search — no results (discovery falls back to model knowledge)"
        )
    except Exception as exc:
        console.print(f"[yellow]WARN[/] web search — {exc}")

    if track and settings.use_audio:
        try:
            from .audio import analyze_track

            f = analyze_track(track)
            if f:
                console.print(f"[green]OK[/] audio — {f.summary()}")
                if not f.moods:
                    console.print(
                        "[yellow]WARN[/] mood models unavailable (install the 'mood' extra)"
                    )
            else:
                console.print("[yellow]WARN[/] audio — no features computed")
        except Exception as exc:
            console.print(f"[yellow]WARN[/] audio — {exc}")

    try:
        from .llm import health

        console.print(f"[green]OK[/] LLM ({settings.provider}) — {health()}")
    except Exception as exc:
        ok = False
        console.print(f"[red]FAIL[/] LLM ({settings.provider}) — {exc}")
        if settings.provider == "ollama":
            console.print("      Fix with: make ollama")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    app()
