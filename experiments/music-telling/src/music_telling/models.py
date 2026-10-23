"""Typed records passed between pipeline stages.

These double as the JSON schemas handed to Claude for structured output, so the
field descriptions are load-bearing - they are the only instructions the model
gets about what belongs in each field.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- discovery


class Candidate(BaseModel):
    """A song proposed for the theme, before we've confirmed it exists."""

    title: str = Field(description="Song title only, no artist, no featuring credits.")
    artist: str = Field(description="Primary performing artist.")
    reason: str = Field(description="One sentence on why this song fits the theme.")


class CandidateList(BaseModel):
    # Bounded: an unbounded array lets a constrained-decoding backend emit items
    # forever without ever closing the bracket.
    candidates: list[Candidate] = Field(min_length=1, max_length=60)


class ThemeBrief(BaseModel):
    """The pipeline's reading of the theme - drives search and judging."""

    restatement: str = Field(description="The theme restated in one clear sentence.")
    emotional_arc: str = Field(
        description="The arc a story on this theme should travel, e.g. "
        "'denial -> confrontation -> uneasy peace'."
    )
    motifs: list[str] = Field(
        min_length=3,
        max_length=12,
        description="6-12 concrete lyrical motifs, images or situations that signal "
        "this theme in a song.",
    )
    search_queries: list[str] = Field(
        min_length=2,
        max_length=10,
        description="6-10 complete web search queries that would surface human-curated "
        "SONGLISTS about this theme. Each must read like text typed into a search "
        "engine and must mention songs, e.g. 'songs about leaving home for the first "
        "time' or 'best indie tracks about moving away'. Never put anti-themes here.",
    )
    anti_motifs: list[str] = Field(
        min_length=1,
        max_length=8,
        description="Themes that look superficially similar but should be eliminated.",
    )


# ---------------------------------------------------------------- catalog


class Track(BaseModel):
    """A candidate resolved against Deezer's catalog."""

    title: str
    artist: str
    deezer_id: int | None = None
    album: str | None = None
    isrc: str | None = None
    preview_url: str | None = None
    duration: int | None = None
    rank: int | None = Field(
        default=None, description="Deezer popularity rank; higher is more popular."
    )
    link: str | None = None

    @property
    def slug(self) -> str:
        return f"{self.artist} - {self.title}"


class Lyrics(BaseModel):
    text: str
    source: str = "lrclib"
    instrumental: bool = False

    def excerpt(self, chars: int = 2400) -> str:
        return self.text if len(self.text) <= chars else self.text[:chars] + "\n[...]"


class AudioFeatures(BaseModel):
    """Signal-derived descriptors, computed from the 30s preview."""

    tempo: float | None = None
    key: str | None = None
    mode: str | None = None
    energy: float | None = Field(default=None, description="Mean RMS loudness, 0-1ish.")
    brightness: float | None = Field(
        default=None, description="Spectral centroid in Hz; timbral 'brightness'."
    )
    dynamics: float | None = Field(
        default=None, description="Std-dev of RMS; how much the track breathes."
    )
    onset_rate: float | None = Field(
        default=None, description="Note onsets per second; rhythmic busyness."
    )
    moods: dict[str, float] = Field(
        default_factory=dict,
        description="Essentia MusiCNN head probabilities, 0-1 per mood.",
    )

    def summary(self) -> str:
        bits = []
        if self.tempo:
            bits.append(f"{self.tempo:.0f} BPM")
        if self.key and self.mode:
            bits.append(f"{self.key} {self.mode}")
        if self.energy is not None:
            bits.append(f"energy {self.energy:.2f}")
        if self.brightness is not None:
            bits.append(f"brightness {self.brightness:.0f}Hz")
        if self.dynamics is not None:
            bits.append(f"dynamics {self.dynamics:.2f}")
        if self.moods:
            # Only surface moods the classifier is actually confident about.
            # Listing "party 0.01" is noise that misleads the judge.
            present = [(k, v) for k, v in self.moods.items() if v >= 0.5]
            top = sorted(present, key=lambda kv: -kv[1])[:4]
            if top:
                bits.append("mood: " + ", ".join(f"{k} {v:.2f}" for k, v in top))
        return "; ".join(bits) if bits else "no audio analysis"


class EnrichedTrack(BaseModel):
    track: Track
    lyrics: Lyrics | None = None
    features: AudioFeatures | None = None
    reason: str = ""

    def brief(self, lyric_chars: int = 2400) -> str:
        """The text block handed to the LLM judge."""
        parts = [
            f"TITLE: {self.track.title}",
            f"ARTIST: {self.track.artist}",
            f"ALBUM: {self.track.album or 'unknown'}",
            f"AUDIO: {self.features.summary() if self.features else 'not analyzed'}",
        ]
        if self.lyrics and not self.lyrics.instrumental:
            parts.append(f"LYRICS:\n{self.lyrics.excerpt(lyric_chars)}")
        else:
            parts.append("LYRICS: unavailable or instrumental")
        return "\n".join(parts)


# ---------------------------------------------------------------- judging


class Judgement(BaseModel):
    """One song scored against the theme."""

    theme_alignment: int = Field(
        ge=0,
        le=100,
        description="How directly the song embodies the theme. 0 = unrelated, "
        "100 = the theme is the song's explicit subject.",
    )
    evidence: list[str] = Field(
        max_length=3,
        description="1-3 short verbatim lyric quotes that justify the score. "
        "Quote exactly; if lyrics were unavailable, return an empty list.",
    )
    narrative_role: str = Field(
        description="Where this song would sit in a story on the theme, e.g. "
        "'the denial before the break', 'the quiet aftermath'.",
    )
    distinctness: int = Field(
        ge=0,
        le=100,
        description="How much this song adds that other theme songs would not. "
        "Penalize songs that are interchangeable with a more famous entry.",
    )
    justification: str = Field(description="Two sentences defending the score.")
    keep: bool = Field(description="Whether this song deserves a place in the shortlist.")


class ScreenVerdict(BaseModel):
    """Cheap first-pass keep/cut on title+artist+reason alone."""

    keep_indices: list[int] = Field(
        max_length=60,
        description="0-based indices of candidates worth the expense of full "
        "enrichment. Cut duplicates, wrong-theme entries, and novelty tracks.",
    )
    cut_rationale: str = Field(description="One sentence on what you eliminated and why.")


class ScoredTrack(BaseModel):
    enriched: EnrichedTrack
    judgement: Judgement

    # Distinctness may only break ties, never overturn a real alignment gap.
    DISTINCTNESS_BONUS: ClassVar[float] = 5.0

    @property
    def score(self) -> float:
        """Theme alignment is the ranking; distinctness is a tie-breaker.

        The bonus is capped well below the granularity of alignment scores, so a
        song can never win on distinctness while being materially less on-theme.
        """
        return (
            self.judgement.theme_alignment
            + (self.judgement.distinctness / 100.0) * self.DISTINCTNESS_BONUS
        )


# ---------------------------------------------------------------- story


class StoryBeat(BaseModel):
    position: int = Field(description="1-based position in the story.")
    title: str = Field(description="Song title, copied exactly from the input.")
    artist: str = Field(description="Artist, copied exactly from the input.")
    beat_name: str = Field(description="A short name for this narrative beat.")
    prose: str = Field(
        description="2-4 sentences advancing the story, written so it reads as "
        "continuous narrative with the beats around it. Reference the song's actual "
        "lyrics and its sonic character."
    )
    quote: str = Field(
        description="One verbatim lyric line from this song that carries the beat. "
        "Empty string if lyrics were unavailable."
    )
    transition: str = Field(
        description="One sentence handing off to the next song. Empty for the last beat."
    )


class Story(BaseModel):
    title: str = Field(description="An evocative title for the whole story.")
    logline: str = Field(description="One sentence describing the arc.")
    beats: list[StoryBeat] = Field(min_length=1, max_length=20)
    coda: str = Field(description="A short closing paragraph landing the theme.")


class StoryOrder(BaseModel):
    """Round 3: final selection and sequencing."""

    chosen: list[int] = Field(
        max_length=20,
        description="0-based indices of the finalists, in the exact order they "
        "should be told. Length must equal the requested story length.",
    )
    arc_rationale: str = Field(
        description="Why this order produces a coherent arc, and what the cut songs "
        "were redundant with."
    )
