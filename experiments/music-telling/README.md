# music-telling

**Status: experiment.** A standalone CLI, not built on Sage; nothing in the monorepo calls it.

Turn a **theme** into a coherent **story told through songs** — discovered from the
open web, enriched with real lyrics and real audio analysis, then ranked and
eliminated so only the songs that genuinely carry the theme survive.

```
theme ──▶ brief ──▶ discover ──▶ screen ──▶ resolve ──▶ enrich ──▶ score ──▶ eliminate ──▶ sequence ──▶ story
           LLM      LLM + web     LLM       Deezer     LRCLIB +     LLM        rules         LLM         LLM
                                                       librosa +   judge                   editor      writer
                                                       Essentia
```

## Why it's built this way

Spotify retired its `audio-features` endpoint for new applications, so "danceability"
and "valence" are no longer something you can look up. This project computes the
equivalent itself: it pulls the free 30-second preview from Deezer and runs
**librosa** (tempo, key, energy, brightness, dynamics, onset rate) plus
**Essentia's MusiCNN** mood classifiers over the actual waveform. Every data source
is open and key-free; an Anthropic key is optional, for the `--provider anthropic`
reasoning backend.

**It runs entirely on free, open components — no API key, no account.** The
reasoning stages use a local open-weights model through [Ollama]; the whole
pipeline works offline once the model and lyrics are cached.

| Stage | What it uses | Key needed |
|---|---|---|
| Song discovery | DuckDuckGo (`ddgs`) + local LLM | none |
| Catalog resolution | Deezer public API | none |
| Lyrics | LRCLIB | none |
| Audio features | librosa | none |
| Mood / danceability | Essentia MusiCNN (MSD) | none |
| Ranking, elimination, story | Ollama (`qwen2.5:7b`) | none |

[Ollama]: https://ollama.com

## Quick start

```bash
make setup-mood   # .venv from uv.lock, incl. Essentia mood models
make ollama       # install Ollama + pull qwen2.5:7b (~4.7GB, one time)
make doctor       # verify every source and the local model
make tell THEME="leaving home for the first time"
```

No key, no signup. `make ollama` installs the runtime, starts it, and pulls the
model into `~/.ollama`.

The story is written to `out/story.md`. Add `--json out/run.json` for the full
record: every candidate, every score, and every elimination.

```bash
uv run music-telling tell "the moment you realise your parents are people" \
    --length 7 --pool 50 --out out/story.md --json out/run.json
```

### Swapping the model

Any Ollama model works — bigger is better if you have the RAM:

```bash
ollama pull llama3.1:8b
make tell MODEL=llama3.1:8b THEME="..."          # or --model llama3.1:8b
```

Claude remains available as an opt-in backend:

```bash
export ANTHROPIC_API_KEY=sk-...
uv run music-telling tell "..." --provider anthropic
```

### Without the mood models

Essentia pulls in TensorFlow. To skip it, `make setup` and pass `--no-mood`
(librosa features still run) or `--no-audio` (metadata + lyrics only, fastest).

## How the ranking works

The goal is *high alignment to theme*, so the pipeline eliminates in four passes,
cheapest first:

1. **Screen** — on title/artist/reason alone, before spending any bandwidth. Cuts
   duplicates, anti-motif matches, and title-only connections.
2. **Resolve** — anything not findable in the catalog is dropped.
3. **Score** — each survivor is judged on full lyrics *and* its audio analysis.
   The judge must quote **verbatim lyric evidence**; a song whose lyrics couldn't
   be fetched is capped at 55, so unverifiable songs can't win.
4. **Eliminate + sequence** — songs below the alignment threshold are cut, then a
   final editing pass picks the best arc, dropping high scorers that are
   redundant with a stronger neighbour.

Ranking is `theme_alignment` plus a bounded `distinctness` tie-breaker (max 5
points). Distinctness settles near-ties but can never make a less on-theme song
outrank a more on-theme one.

## Commands

| Command | Does |
|---|---|
| `make help` | List all targets |
| `make setup` / `setup-mood` | Create `.venv` from `uv.lock` (`uv sync --locked`) |
| `make vuln` / `secrets` / `imports` | pip-audit of `uv.lock` / the repo's gitleaks scan / n/a |
| `make image-test` | Build the lite image, run `--help` and the tests in it offline, remove it |
| `make tell THEME="..."` | Build a story (`LENGTH=`, `POOL=`, `OUT=`) |
| `make tell-fast THEME="..."` | Skip audio analysis |
| `make doctor` | Health-check every source and credential |
| `make test` / `lint` / `fmt` | Test suite, ruff |
| `make clean-cache` | Drop cached lyrics, previews and model weights |

## Docker

```bash
cp .env.example .env        # optional: only for the Anthropic backend
make docker-build
make up                     # runs `doctor` in the container
make tell-docker THEME="songs about a city at 4am"
```

The code is baked into the image (Colima cannot see this folder), so rebuild after
edits; `tell-docker` prints the Markdown on stdout. A named volume keeps the cache
(lyrics, previews, ~25MB of model weights) across runs, and `make down` keeps the
volumes. Ollama is published on `127.0.0.1` only; set `MT_OLLAMA_PORT` if a host
Ollama already holds 11434. The default image is `lite`; `MT_TARGET=full` adds
TensorFlow.

> On Apple Silicon the Essentia wheels are amd64, so the `full` image runs under
> emulation. For native speed locally, use `make setup-mood` instead of Docker.

## Caching

Every HTTP response, audio analysis and model file is cached under
`~/Library/Caches/music-telling` (override with `MUSIC_TELLING_CACHE`). Re-running
a theme is cheap; only the LLM calls repeat. On the Anthropic backend the theme
brief is marked with `cache_control`, so the prefix shared by every per-song
scoring call is served from the prompt cache.

## Working with a small local model

A 7B model is not a frontier model, and the pipeline is built around that:

- **Constrained decoding.** Every stage requests a JSON schema via Ollama's
  `format`, so output shape is enforced during generation rather than hoped for.
- **Flattened schemas.** `$ref`/`$defs` are inlined before being sent — the
  grammar backend handles them inconsistently.
- **Validation retries.** A response that fails Pydantic validation is retried
  with the specific field errors fed back to the model.
- **Tolerant parsing.** Code fences, `<think>` blocks and trailing chatter are
  stripped before parsing.
- **Shorter excerpts.** Lyrics are trimmed to 1400 chars locally (2400 on a
  frontier model), and `num_ctx` is set to 16384 — Ollama's 4096 default would
  silently truncate the prompt.

Expect solid song selection and serviceable prose. If the story reads flat, the
cheapest upgrade is a bigger local model.

## Layout

```
src/music_telling/
  config.py       settings, Essentia model URLs
  models.py       pydantic records — also the LLM's output schemas
  cache.py        content-addressed disk cache
  sources/        deezer, lrclib, websearch, http
  audio/          download, librosa features, Essentia moods
  llm/            client, discover, judge, compose
  pipeline/run.py the stages, wired
  render.py       Markdown output
  cli.py          `tell` and `doctor`
```

## Notes and limits

- Audio analysis reflects the **30-second preview**, not the full recording — good
  for timbre and mood, approximate for songs that change character late.
- LRCLIB coverage is strong for popular music and thin for the obscure; those
  songs are scored on metadata alone and capped accordingly.
- Mood classifier probabilities below 0.5 are suppressed rather than shown, since
  reporting `party 0.01` misleads the judge.
