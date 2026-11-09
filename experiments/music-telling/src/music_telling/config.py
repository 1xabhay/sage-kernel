"""Runtime configuration, resolved from environment with sane defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_cache_dir


def _load_dotenv() -> None:
    """Read a local .env if present, without adding a dependency.

    Real environment variables always win, so an exported value is never
    silently overridden by a stale file.
    """
    for directory in (Path.cwd(), *Path.cwd().parents):
        candidate = directory / ".env"
        if candidate.is_file():
            break
        if (directory / ".git").exists():
            return
    else:
        return
    try:
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    except OSError:
        pass


_load_dotenv()

# Which LLM backend drives discovery, judging and composition.
#   ollama    - local, free, open weights (default)
#   anthropic - Claude, needs ANTHROPIC_API_KEY
DEFAULT_PROVIDER = os.getenv("MUSIC_TELLING_PROVIDER", "ollama")

# Per-provider default model; MUSIC_TELLING_MODEL overrides either.
_PROVIDER_DEFAULT_MODEL = {
    "ollama": "qwen2.5:7b",
    "anthropic": "claude-opus-5",
}


def _default_model(provider: str) -> str:
    explicit = os.getenv("MUSIC_TELLING_MODEL")
    if explicit:
        return explicit
    return _PROVIDER_DEFAULT_MODEL.get(provider, "qwen2.5:7b")


USER_AGENT = "music-telling/0.1 (+https://github.com/local/music-telling)"

# Essentia pretrained models. `embeddings` produces the MusiCNN feature vectors
# that every classification head below consumes.
ESSENTIA_BASE = "https://essentia.upf.edu/models"
EMBEDDING_MODEL = {
    "name": "msd-musicnn-1.pb",
    "url": f"{ESSENTIA_BASE}/feature-extractors/musicnn/msd-musicnn-1.pb",
    "output": "model/dense/BiasAdd",
}
# Each head is a 2-class softmax; index 0 is the positive class.
MOOD_HEADS = {
    "happy": "mood_happy",
    "sad": "mood_sad",
    "aggressive": "mood_aggressive",
    "relaxed": "mood_relaxed",
    "party": "mood_party",
    "acoustic": "mood_acoustic",
    "electronic": "mood_electronic",
    "danceable": "danceability",
}


def mood_head_url(slug: str) -> str:
    return f"{ESSENTIA_BASE}/classification-heads/{slug}/{slug}-msd-musicnn-1.pb"


@dataclass
class Settings:
    provider: str = DEFAULT_PROVIDER
    model: str = field(default_factory=lambda: _default_model(DEFAULT_PROVIDER))
    cache_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("MUSIC_TELLING_CACHE", user_cache_dir("music-telling"))
        )
    )
    # Pipeline shape
    # Defaults suit a local 7-8B model, which degrades if asked for a very long
    # list in one structured response; --pool raises them, and switching to a
    # frontier provider raises them automatically.
    candidate_pool: int = 30  # how many songs discovery aims for
    shortlist_size: int = 14  # survivors of the coarse screen
    story_length: int = 8  # songs in the final story
    screen_threshold: int = 45  # 0-100; below this a song is eliminated in round 2
    max_workers: int = 8
    # Toggles
    use_audio: bool = True
    use_mood: bool = True
    # LLM shape. Local 7-8B models need a bigger explicit context than Ollama's
    # 4096 default, and shorter lyric excerpts than a frontier model.
    num_ctx: int = int(os.getenv("MUSIC_TELLING_NUM_CTX", "16384"))
    max_retries: int = 3

    @property
    def is_local(self) -> bool:
        return self.provider == "ollama"

    @property
    def llm_workers(self) -> int:
        """Concurrency for LLM calls.

        Ollama serves requests from one model near-serially and duplicates the KV
        cache per parallel slot, so fanning out 8 ways adds memory pressure
        without adding throughput. Network and audio work still use max_workers.
        """
        return 2 if self.is_local else self.max_workers

    @property
    def web_context_chars(self) -> int:
        """Budget for web grounding, sized to the context window."""
        return 6000 if self.is_local else 20000

    @property
    def lyric_chars(self) -> int:
        """How much of a lyric to show the judge."""
        return 1400 if self.is_local else 2400

    def use_provider(self, provider: str, model: str | None = None) -> None:
        """Switch backend, resetting the model default unless one is given.

        Pool sizes move with the backend: a frontier model handles a wider
        candidate pool in one response than a local 7B does.
        """
        was_default_pool = self.candidate_pool == Settings.candidate_pool
        was_default_shortlist = self.shortlist_size == Settings.shortlist_size
        self.provider = provider
        self.model = model or _default_model(provider)
        if provider == "anthropic":
            if was_default_pool:
                self.candidate_pool = 40
            if was_default_shortlist:
                self.shortlist_size = 18

    @property
    def audio_dir(self) -> Path:
        return self.cache_dir / "audio"

    @property
    def models_dir(self) -> Path:
        return self.cache_dir / "models"

    @property
    def http_dir(self) -> Path:
        return self.cache_dir / "http"

    def ensure_dirs(self) -> None:
        for d in (self.audio_dir, self.models_dir, self.http_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
