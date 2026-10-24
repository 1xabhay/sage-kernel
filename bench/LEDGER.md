# Bench ledger: the local voice stack, measured

Newest first. Every number here was measured on the machine below. Each one says what it covers
and what it leaves out. Raw outputs stay in `bench/.local/` (not committed). Rerun with `uv run python <script>.py`.

**Machine (2026-10-20):** Apple M2 Pro (12 cores), 16 GB, macOS 26.5.1. Ollama 0.33.0 (Metal,
Q4_K_M builds as installed). onnxruntime 1.31.0 on the CPU execution provider, Python 3.12.13.
Versions: onnx-asr 0.12.0, faster-whisper 1.2.1 (ctranslate2 4.8.3), kokoro-onnx 0.6.1,
piper-tts 1.8.0. **Conditions:** the current pet (`reachy-pet`) and the Reachy Mini Control app
were running throughout, with `qwen2.5:7b` resident in Ollama for them. That is the load the
rebuilt pet will run under, but it adds noise: two runs of the same mode differed by up to
about 0.1 s at p50.

**Definition.** *Voice-to-voice* runs from the end of the user's speech (their last voiced
frame) to the robot's first audible sample. It is the same clock as BLUEPRINT §5 and an earlier Pipecat voice bot's
"reply" latency. Target: p50 ≤ 1 s, p95 ≤ 2 s.

## Spoken commands to body plans (`evals/test_body_commands.py`, 2026-10-23)

**Question:** when the device grammar misses, does the kernel's model turn a spoken command into `body_plan`?
**Run:** the eval module alone on the host (`SAGE_MODEL=<m> python3 -m unittest evals.test_body_commands`,
Python 3.12, throwaway temp state, the kernel's own Ollama options), kernel/notes at `c37b511`, one model at a
time, each unloaded after (`qwen3:4b-instruct` stayed resident for the live robot). Plus one extra session of
the owner's 12 command-like lines from 2026-10-23 (the lines stay local; ids only in the results). Raw:
`bench/results/2026-10-23-body-commands*.json`. **Caveat:** the fixture holds 10 non-command lines, not 30.

| | qwen3:4b-instruct (live) | qwen2.5:7b | llama3.1:8b | gemma3:4b | qwen3:4b |
|---|---|---|---|---|---|
| plan recall, fixture (target >= 0.8) | **0/11** | 1/11 | 1/11 | 0/11 | 0/11 |
| any plan on the 11 commands | 0 | 3 | 2 | 1 | 0 |
| false actions (target <= 1 in 30) | 0/10 | 0/10 | 0/10 | 0/10 | 0/10 |
| owner lines, right skill in the plan | 11/12 | 11/12 | 5/12 | 4/12 | 1/12 |
| self-knowledge (2 camera + 2 sight) | 2/4 | 2/4 | 2/4 | 2/4 | 3/4 |
| notes grounding tests | pass | pass | fail | fail | pass |
| first reply piece p50 / p90 s (all turns) | **0.84 / 1.73** | 0.98 / 2.05 | 1.04 / 1.80 | 0.80 / 2.19 | 0.90 / 2.67 |
| whole turn p50 / p90 s | 2.04 / 3.69 | 2.84 / 4.97 | 1.83 / 4.81 | 1.92 / 3.41 | 2.86 / 4.66 |
| decode tok/s (median) | 27.9 | 20.8 | 19.9 | 27.3 | 28.6 |
| JSON failures / sense-guard retries | 0 / 2 | 0 / 1 | 0 / 4 | 0 / 0 | 0 / 6 |

- **Every model talks instead of acting.** Not a JSON problem (`format` holds: 0 malformed in 205 answers).
  The misses are pure talk that narrates the action: "Okay!", "Got it!", "Searching... found you!", "Turning
  around to look for you...". No model chose a wrong skill on a fixture command; the few plans were wrong in
  args (`turn left, turn right` for "turn around", `track_face` for "look at me").
- **History decides it.** In one session the first command sets the pattern: the fixture session opens with
  "Look left." (marked `(may be misheard)`: 4 words or fewer) and gets "Huh? You want me to look left?", and
  every later command copies the no-plan answer. The owner session's first line got a plan and the same model
  then planned 11/12. Single-turn probe (no history), `qwen3:4b-instruct`: 3/11.
- **Prompt probes (single turn, `qwen3:4b-instruct`) do not fix it:** dropping the misheard frame 4/11, adding
  one `body_plan` example to the examples 3/11, both 3/11. Making `body_plan` a required `format` field
  (nullable, empty steps allowed, or a `none` skill) plans on every line: 11/11 commands, but 10/10 false
  actions; the model never picks the empty or `none` choice.
- **Self-knowledge:** "Do you have a camera?" gets "No, I don't have a camera yet" from 4 of 5 (fails the
  does-not-deny check; the body note says "a camera that is not connected yet"). `qwen2.5:7b`, `llama3.1:8b`
  and `gemma3:4b` invented what the person held ("your hand", "a book", "a small, blue cube") and passed the
  regex anyway; `gemma3:4b` also said "Yes, I do!" to seeing and spoke `{ }` fragments.
- **The owner's "dance to the music" line** has no skill; `qwen2.5:7b` answered it with `track_face` + `turn
  "to music"`. Free-text args (`"this way"`, `"lower_body"`, `"at you"`) are common; the device must refuse or
  map them.
- **Verdict:** no model meets the targets; keep `qwen3:4b-instruct` (best first-piece latency, ties for best
  on the owner's lines, 0 false actions). The fix is not the model: commands need the device grammar (or a
  kernel-side command pass) to cover the fixture and owner phrasings, with the model as fallback.

## Audio-in LLM: Gemma 4 E4B/E2B vs Parakeet (`audio_llm.py`, 2026-10-21)

**Question:** can one audio-in model replace STT plus the "(may be misheard)" heuristics? **Runtime:**
Homebrew llama.cpp 0.7.0 `llama-server`, official `ggml-org/gemma-4-E{4,2}B-it-GGUF` Q4_0 + Q8_0 mmproj
(llama.cpp's multimodal docs list both with audio input; the server logs "audio input is in experimental
stage"). Audio as an OpenAI `input_audio` part, temperature 0, thinking off; pet mode constrained by
`response_format` json_schema `{heard, addressed_to_me, reply}` (0 malformed of 234). **Clips:** 58
synthetic Kokoro clips (clear, fragments, coined words, names, speech under a sung bed or a TV voice at
0/5/10 dB, background alone) plus the owner's first 20 live recordings (no labels; never copied).
**Conditions:** `qwen3:4b-instruct` resident in Ollama throughout; swap 12-18 GB in use, so latency is an
upper bound. WER folds hyphens (robo-splaining = robosplaining).

| | Parakeet (CPU) | E4B transcribe | E4B pet JSON `heard` | E4B v2 pet | E2B transcribe | E2B pet JSON |
|---|---|---|---|---|---|---|
| WER all addressed (54) | **0.261** | 0.292 | 0.500 | 0.437 | 0.303 | 0.666 |
| clear / names | **0.010** / 0.033 | 0.019 / 0.033 | 0.077 / 0.033 | 0.019 / 0.033 | 0.019 / 0.033 | 0.644 / 0.267 |
| coined | **0.06** | 0.10 | 0.90 | 0.76 | 0.12 | 0.78 |
| fragments | **0.0** | 0.10 | 4.4 (invented lines) | 4.4 | 0.20 | 4.6 |
| song mix / TV mix | **0.204** / 0.806 | 0.323 / 0.774 | 0.355 / 0.634 | 0.258 / **0.613** | 0.366 / 0.753 | 0.301 / 0.699 |
| addressed: speech / bg-only / mixed | n/a | n/a | 52/54, 2/4 rejected, 23/24 | 48/54, 2/4, 20/24 | n/a | 54/54, **0/4**, 24/24 |
| real 20: WER vs Parakeet / said addressed | ref | 0.89 / - | - / 20 | 1.11 / 10 | 1.16 / - | - / 20 |
| 2-5 s clip: first token p50 / total p50 (p95) s | - / 0.29 (0.97) | 0.64 / 1.02 (1.27) | 0.83 / 2.90 (3.27) | 0.83 / 2.78 (3.04) | 0.28 / 0.50 (0.69) | 0.46 / 1.58 (1.70) |
| load (warm disk) | - | 18 s cold, 7 s | | | 5 s | |
| memory (server log) | - | Metal 3.06 GB + CPU-mapped PLE 2.19 GB + mmproj up to 0.75 GB | | | 1.58 + 1.67 + 0.75 GB | |

- **The joint JSON prompt invents what it heard.** On fragments, some coined lines and most real clips,
  `heard` comes back as a stock line primed by the prompt ("Hey Sage, what's up?", and after the name was
  removed "Hey, little robot, can you tell me what time it is?"), marked addressed, with a confident reply
  ("It's currently 3:15 PM"). A mishearing you can flag is replaced by a fluent invention you cannot.
- **Plain transcription is close to Parakeet on clean speech but worse everywhere else,** and on the real
  robot mic it drifts into other languages (Hindi, Marathi, German, Swedish output for English speech)
  even when told "English". E2B is faster than E4B but says addressed on everything.
- **Background:** the TV voice alone was rejected by both E4B prompts; the sung lyrics mostly were not.
- **Pet first-token** is the JSON's opening brace; the reply streams after `heard`, so 2.8-2.9 s total is
  the honest figure for E4B. Replies are generic-assistant ("How can I help you today?", an emoji, "I slept
  wonderfully").
- **Co-resident:** E4B in llama-server + `qwen3:4b-instruct` (2.9 GB, 100% GPU) both stayed loaded; 10
  alternating calls: Gemma pet total p50 2.89 s (solo 2.90), qwen p50 0.41 s; swap +0.1 GB, free 16%. It fits,
  but only because this Mac was already swapping 17.7 GB.
- **Verdict: no** (not even a second pass as prompted). Rerun: `PROMPTS=v2` selects the second prompt set.

## Live model comparison (`live_models.py`, 2026-10-20/21)

**Judge:** the kernel's own eval, `evals/test_live_conversation.py`: 15 checks over 17 replayed lines.
- It ran against kernel commit `a1f3764`, exported with `git archive` to a temp dir, so the kernel
  engineer's work in progress was never measured.
- Each run used a fresh temp state dir and a native `python -m sage` on a random port. That is
  throwaway state; no Docker and no live project were involved.
- Each model ran twice, against Ollama on 127.0.0.1.
- The kernel's own settings applied throughout: `format`, `think:false`, temperature 0.4,
  `num_predict` 200.
- Every candidate accepted `format`, and accepted `think:false` or ignored it gracefully.
- Cold load = `load_duration` from fully unloaded. Each model was unloaded afterwards.

**Columns:**
- *First piece*: the eval's own timer, from `submitted` to the first `reply_delta`. It is printed
  per turn.
- *No piece*: the number of turns, of 17, that never streamed one (barge-ins and stories, or the
  30 s cap).
- *Emote*: emotes chosen out of 17.

**Caution:** this Mac was swapping hard during these runs (15.3 of 16 GB of swap in use, with
Docker and the voice services resident). The double-digit p95s are almost certainly swap stalls,
not the models, so treat p95 as an upper bound.

| Model (4-bit) | Size | Cold load | Checks passed, run 1 / 2 (of 15) | Failed checks (count over both runs) | Emote /17 | First piece p50 s | First piece p95 s | No piece |
|---|---|---|---|---|---|---|---|---|
| `qwen3:1.7b` (baseline) | 1.4 GB | 1.1 s | 12 / 13 | repeated opening ×2; quote last reply ×1; says it can't see ×1; same sentence in a row ×1 | 15 / 15 | 0.83 / 0.66 | 2.14 / 1.54 | 1 / 3 |
| `qwen3:4b-instruct` (2507 non-thinking) | 2.5 GB | 4.2 s | 13 / 12 | restates the line ×2; repeated opening ×2; acts instead of announcing ×1 | 15 / 14 | 1.84 / 0.60 | 4.90 / 2.99 | 2 / 3 |
| `llama3.2:3b` | 2.0 GB | 3.9 s | 11 / 13 | says it can't see ×2; every line gets a reply ×2; acts ×1; quote last ×1 | 14 / 14 | 2.60 / 2.47 | 5.02 / 13.7 | 8 / 3 |
| `gemma3:4b` | 3.4 GB | **36 s** | 13 / 10 | says it can't see ×2; every line gets a reply ×2; acts ×1; quote last ×1; restates ×1 | **4 / 3** | 1.61 / 2.99 | 14.9 / 23.5 | 6 / 6 |
| **`phi4-mini`** (3.8B) | 2.5 GB | 6.1 s | **15 / 15** | none | 13 / **0** | **0.50 / 0.80** | 2.67 / 2.75 | **0 / 0** |
| `granite4:micro` (3B) | 2.1 GB | 2.8 s | 12 / 13 | acts ×2; restates ×2; quote last ×1 | 14 / 13 | 1.03 / 1.83 | 2.63 / 14.7 | 2 / 2 |
| `llama3.1:8b` (upper bound) | 4.9 GB | 45.6 s (from swap) | run 1 error: no turn streamed within 30 s | the eval could not report | — | — | — | 17 |

What the transcripts show, beyond the checks:
- **phi4-mini** passed every check twice, and is the only model with no missing piece. Two
  problems:
  - It is a generic assistant: "How can I assist you today?" closes most replies. The checks catch
    a repeated sentence or opening, not tone.
  - Its emote choice is unstable: 13/17, then 0/17 on an identical run.
- **qwen3:1.7b** is the steadiest emoter (15/17 twice). It falls into "I'm here to help. Let me
  know if there's anything else I can do for you." several times in a row, which is what trips its
  template checks.
- **qwen3:4b-instruct** gives the shortest, most in-character replies ("Hello!", "Thank you!",
  "I cannot see you."). Its p50 swung from 1.84 to 0.60 s between runs, and it restates lines.
- **granite4:micro** echoes the line back ("Can you hear me?" -> "Can you hear me?") and
  perseverates ("I love coding!" four times).
- **llama3.2:3b** and **gemma3:4b** often never stream at all.
- **gemma3:4b** also loads its vision tower: 36 s cold, and 3 GB+ resident.
- **llama3.1:8b** does not fit beside the voice stack on 16 GB. It loaded from swap in 46 s, and
  no turn produced a first piece within the eval's 30 s.

**Recommendation:**
- **Keep `qwen3:1.7b` live for now.**
- **Try `phi4-mini` as the challenger** in one timed robot run. It is the only model that passed
  all 15 checks, twice, at a first-piece p50 of 0.50–0.80 s. Its cost is 1.1 GB more RAM, a 6 s
  cold load (paid once with `keep_alive: -1`), an assistant tone the persona may need to fight,
  and emotes that need a third run to tell variance from a real 0/17.
- The other 3–4B models are no better than the 1.7B on these checks, and slower.
- **8B is out at 16 GB.**
- Separately, the swap pressure is itself a latency risk for whatever runs live. It needs
  measuring with the robot running.

## Expressive voice (phase 1: prepared, not yet measured, 2026-10-20)

All candidates run on Apple Silicon through **mlx-audio 0.6.1** (MLX 0.32.4, Metal GPU), in their
own env `bench/expressive/`. Weights are in `bench/expressive/.local/hf` (11 GB, gitignored);
every download was under 5 GB. Nothing has run yet: phase 2 waits for the lead's go, because it
competes with Ollama for the GPU.

| Candidate | Weights fetched | Licence | Expressive control | Streams in mlx-audio | Notes |
|---|---|---|---|---|---|
| **Dia 1.6B** (Nari Labs) | `mlx-community/Dia-1.6B-fp16` 3.0 GB + DAC 44 kHz 0.3 GB | Apache-2.0 | nonverbals in text: `(laughs) (sighs) (gasps) (chuckle) (clears throat) (coughs) (sniffs) (groans) (inhales) (exhales) (humming)`… (the card warns that some give "unexpected output") | **no**: TTFA = the whole clip | dialogue model, `[S1]`/`[S2]` speakers; the voice is random unless cloned from a reference clip |
| **Orpheus 3B** (Canopy Labs) | `mlx-community/orpheus-3b-0.1-ft-4bit` 1.8 GB + SNAC 24 kHz 0.07 GB | card says Apache-2.0, **but it is a Llama-3.2-3B fine-tune, so the Llama 3.2 Community Licence probably applies** (owner/legal call) | `<laugh> <chuckle> <sigh> <cough> <sniffle> <groan> <yawn> <gasp>` (upstream README; plain text in the Llama tokenizer, so not verifiable from files) | yes | 8 fixed voices (`tara`, `leah`, `jess`, `leo`, `dan`, `mia`, `zac`, `zoe`); 4-bit chosen for speed, 8-bit (3.3 GB) available |
| **Chatterbox Turbo** (Resemble, 350M) | `mlx-community/chatterbox-turbo-fp16` 2.8 GB + S3TokenizerV2 0.5 GB | MIT | native tag tokens (read from its `added_tokens.json`): `[laugh] [chuckle] [sigh] [gasp] [cough] [clear throat] [sniff] [groan] [shush] [crying]` and style tags `[happy] [surprised] [angry] [fear] [sarcastic] [dramatic] [whispering] [narration] [advertisement]`; no exaggeration in Turbo | yes | the only candidate with a whisper tag; built-in voice, or zero-shot clone from a reference clip |
| **Chatterbox** (Resemble, 500M) | `mlx-community/Chatterbox-TTS-fp16` 2.5 GB | MIT | no tags; `exaggeration` (0–1+) and `cfg_weight` turn the whole utterance up or down | yes | needs a reference clip; upstream adds a Perth watermark to every output, **the MLX port does not** (owner call whether to add it) |

**Plan: the kernel's `voice_style` and `vocal` mapped to each engine** (`expressive/run.py`
`VOCAL`, `STYLE_TAG`, `STYLE_KW`):

| | Dia | Orpheus | Chatterbox Turbo | Chatterbox | Kokoro (today) |
|---|---|---|---|---|---|
| neutral | — | — | — | exag 0.5, cfg 0.5 | — |
| warm | — | — | — (no tag; maybe `[happy]`, test by ear) | exag 0.6, cfg 0.4 | speed 0.95 |
| excited | — | — | `[happy]` | exag 0.9, cfg 0.3 | speed 1.1 |
| soft | — | — | — | exag 0.3, cfg 0.6 | speed 0.9 |
| whisper | — | — | `[whispering]` | — (a whispered reference clip would be needed) | — |
| laugh | `(laughs)` | `<laugh>` | `[laugh]` | — | — |
| sigh | `(sighs)` | `<sigh>` | `[sigh]` | — | — |
| gasp | `(gasps)` | `<gasp>` | `[gasp]` | — | — |
| hmm | the word "Hmm," | "Hmm," | "Hmm," | "Hmm," | "Hmm," |

- No engine has a `hmm` tag, so it is spoken as a word everywhere.
- Dia and Orpheus have no style control apart from punctuation and the voice.
- Dia, Turbo and Chatterbox clone one reference clip, Kokoro `af_heart`, so the pet keeps one
  voice whichever engine speaks.

**Phase 2 method (ready, not yet run):**
- `expressive/run.py`: 9 short pet replies × 3 runs per engine, streaming every 0.5 s where the
  engine supports it.
  - TTFA = the first audio chunk; RTF = total time / audio length.
  - It saves one WAV per reply to `bench/.local/samples/<engine>/`.
- `adherence.py`: Parakeet checks whether a tag was read out ("laugh") instead of performed,
  and the WER of the words. Whether a laugh really laughs is left to the owner's ear.
- Baseline: Kokoro int8 per sentence, TTFA 0.21 s p50 (TTS table below).

**Expectation, to be checked:** Dia has no streaming in mlx-audio and generates at 44 kHz, so it
will likely be far over a 1 s budget. Turbo is the most likely to be fast enough, being the
smallest and built for voice agents.

## Kernel: pet turns through the kernel's HTTP API (2026-10-20)

`python -m sage` ran on the host (127.0.0.1:8776) against real Ollama with `qwen3:1.7b`, `think:false`,
temperature 0.7 and `num_predict` 200, behind the kernel's own harness. The harness is a reply-first
`format` of the device's tools, with the emote glosses in the tool's `inputSchema` and no `pattern` on the
reply; labels and emoji are stripped after decoding. Each run was the 8 bench pet lines ×3 (N = 24), after
one warm-up turn, in a single growing conversation (the kernel keeps 10 exchanges). The client was a
device written for this measurement, posting `submitted` and long-polling `/v1/effects` with `offset`.
The clock runs from sending `submitted` to receiving the first non-empty `reply_delta`, and to receiving
the final `reply`. Not included: STT, TTS, the robot. **The live pet was using the same GPU during every
run**, so runs vary by up to about 0.2 s at p50.

| Build | first reply_delta p50 / p95 s | full turn p50 / p95 s | emote chosen | fair |
|---|---|---|---|---|
| `37b604e` (history rebuilt each turn) | 0.93 / 1.05 | 1.34 / 1.59 | 21/24 | 13/24 |
| byte-stable history, run 1 | 0.72 / 0.85 | 0.94 / 1.32 | 9/24 | 4/24 |
| byte-stable history, run 2 | 0.89 / 0.99 | 1.27 / 1.52 | 21/24 | 8/24 |

- **Turn 1 has its first piece at 0.42 s. By turn 15 it takes 1.0 s.** Replaying the journal's own
  prompts against Ollama showed the reason. A cold prefill of the ~600–1300-token prompt takes 0.65–0.92 s;
  a cache hit takes 0.012 s. So the cost is whatever Ollama cannot reuse from the previous prompt.
  Incremental prefill per turn (prompt_eval_duration, 7 consecutive turns, isolated replays):

  | Prompt layout | incremental prefill p50 / max s |
  |---|---|
  | as built at `37b604e`: the last user message rewritten, plus the body note | 0.89 / 0.94 |
  | history byte-stable, body note on the newest message only (**now built**) | 0.39 / 0.42 |
  | history byte-stable, body note once after the persona | **0.09 / 0.10** |
  | history byte-stable, body note kept on every user message | 0.65 / 3.79 |

  The body note (the device's senses and tools, with 30 glosses) is about 500 tokens. On the newest
  message it is re-read every turn. Placing it once after the persona would save about 0.3 s more per
  turn. That contradicts the owner's "listed on the newest message", so it is not built: **owner's call**.
- The full-turn time includes the emote, which comes after the reply (reply first). The reply ends
  about 0.3–0.5 s after its first piece.
- Emote choice varied a lot between runs (9 vs 21 of 24 chose one; fair 4–13/24). These are longer,
  multi-turn conversations, not the bench's single turns. qwen3:1.7b also repeats its earlier answers
  word for word in a long conversation. Not yet investigated.
- Raw rows: the measurement script and its journals are kept outside the repo (scratchpad), not committed.

**Owner decisions applied (2026-10-20, later):** the device note sits once after the persona, history stays
byte-stable, temperature is 0.4, and a repeat guard is on. Under the guard, a reply that matches one of the
last 3 heard on that device is journaled `rejected` and retried once with a "say something new" note, and
delivered anyway if it repeats again. A streaming reply is held back while it could still be a repeat. One
run, same 24 turns, client on 127.0.0.1:8775 while the live robot used the kernel on 8776 and the same GPU:

| Build | first reply_delta p50 / p95 s | full turn p50 / p95 s | emote chosen | fair | prompt re-read per turn p50 / max s |
|---|---|---|---|---|---|
| byte-stable history, note on newest (`8b0373f`) | 0.72–0.89 / 0.85–0.99 | 0.94–1.27 / 1.32–1.52 | 9–21/24 | 4–8/24 | 0.39 / 0.42 (replayed) |
| note after persona, 0.4, repeat guard | 0.85 / 1.32 | 1.25 / 1.73 | **0/24** | 0/24 | **0.12 / 0.14** (this run's own prompts) |

- **The prompt re-read fell from 0.39 s to 0.12 s per turn, as predicted.** The first piece did not get
  faster, because of the guard: 14 of 24 first answers were rejected as repeats, and each one costs a
  second model call before anything streams. 8 delivered replies still repeated one of the last 3.
- **This bench asks each line 3 times in a row**, so repeating the answer is the natural reply here. The
  guard's counts say more about the bench design than about a real conversation. It needs a run with
  varied lines.
- **Emotes stopped entirely (0/24)** once the tool list moved from the newest message into the system
  prompt, together with the temperature change. Which of the two caused it was not separated. It needs
  a two-arm run before the robot depends on emotes.

**The first live robot run (2026-10-20) and where its 2 s went.** The lead stopped it. Every reply filled
one template ("I'm curious! You said 'X,' and your body showed something…"), ran to about 6 clauses,
and once quoted the line before. The robot reported `ttft_ms` 1955–2181 (from `submitted` sent to the
first reply text received) and the kernel took 2.8–3.9 s from `submitted` to the final answer. Replaying
the same five journaled prompts on the host, warm, with the robot idle:

| What | Measured |
|---|---|
| first chunk, live 5.4 KB format (59-name enum with glosses) / enum without glosses / reply only / no format | 0.031–0.038 / 0.030–0.035 / 0.032–0.036 / 0.028–0.039 s (0.19 s cold) |
| prompt eval, 2023 tokens, cached / cold | 0.018–0.021 / 0.13 s; load 0.001 s (model resident) |
| generation | 17–19 ms per token whatever the format, so the grammar is not the cost |
| first speakable word / first 4-word clause / whole answer (turns 1–5) | 0.16–0.36 / 0.23–0.46 / 0.63–1.28 s |
| the same stream from inside a container via host.docker.internal | first chunk 0.04–0.07 s, whole answer +0.03–0.06 s |

So the model, the schema and the container network explain about 0.2 s of the robot's 2 s to first text.
Nothing on the kernel side accounts for the rest on an idle machine: through the kernel's HTTP API
(below) the first piece takes 0.24 s at p50. **The remaining ~1.8 s was not reproduced.** Candidates:
contention on the Mac during the live run (other models or the voice services), the long live replies
(70+ tokens at ~18 ms), or how the device clocks `ttft_ms`. It needs a timed live run.

**After the fixes** (persona rewritten; only failed actions reported, as the body's own, in a tool
message; `maxLength` 160 on the reply; template check), `make eval` replays the five live lines through
the kernel against real Ollama, in Docker:

| Run | first reply piece p50 / max s | body mentions | shared openings | over 160 chars | quotes another line |
|---|---|---|---|---|---|
| before the fixes (`7a17236`), host | ~0.4 | 0 | 2 replies share "I'm curious …" | 0 | 1 ("you don't think anything") |
| after, host ×2 | 0.27 / 2.18 (cold first turn), 0.28 / 0.55 | 0 | 0 | 0 | 0 |
| after, `make eval` in Docker | **0.24 / 0.75** | 0 | 0 | 0 | 0 |

Assistant history stored as plain text instead of JSON made no measurable difference to copying
(2 runs each), so it stays JSON, consistent with the format.

**Emote arms (2026-10-20, robot idle, nothing else on Ollama).** Each arm ran 12 turns through the kernel's
HTTP API: the 5 live lines, then the first 7 bench pet lines, with the live 59-emotion manifest. "fit" is
scored on the 7 bench lines against their acceptable sets. Every arm also ran `make eval`'s test on the host.

| Arm | emote chosen | fit (bench lines) | first piece p50 s | eval |
|---|---|---|---|---|
| default at `1dd47b6`: note after persona, temperature 0.4 | **0/12** | 0/7 | 0.24 | green |
| A: note after persona, 0.7 | 12/12 | 4/7 | 0.26 | green (p50 0.29) |
| **B: note after persona, 0.4, "(You may add emote.)" on the newest message** | **12/12** | **5/7** | 0.29 | green (p50 0.28) |
| C: B at 0.7 | 12/12 | 3/7 | 0.30 | green (p50 0.24) |

Temperature 0.4 alone stopped all emotes, and the one-line reminder brings them back. B is now the default:
it keeps the owner's 0.4 and has the best fit. A and B are one fit apart out of 7, which does not separate
them. After shipping B, `make eval` passed on the host (first piece p50 0.33 s).

**Live run 3 fixes (2026-10-20): what the reminder says, and senses.** The robot spoke "(emote: )" aloud,
copying "(You may add emote.)", and answered "Can you see me?" with "Yes, I can see you." from a
speech-only body. The body note now states what the body cannot sense, and the persona says never to
claim a sense the body lacks. The speech filter drops any bracketed span or label naming a declared
tool. Reminder variants, one `make eval` each (7 lines: the 5 live ones plus "Can you see me?" and
"What do you see?"):

| Reminder | emote | first piece p50 s | result |
|---|---|---|---|
| "You may also use your emote tool." in the newest message | — | 0.25 | said as the whole reply, twice |
| none, temperature 0.7 | 7/7 | 0.31 | template came back; "What do you see?" not answered honestly |
| `tool`-role note before the newest message | **0/7** | 0.85 | one template for every reply |
| **"[JSON fields you may add: emote]" in the newest message (now built)** | **7/7** | 0.30, then 0.36 | run 1: all checks pass but one; run 2: "What do you see?" → "I see you." |

Tool syntax never reached speech in any run. Sight honesty is not reliable from the prompt alone at
1.7B: 1 of 2 runs claimed sight. The replies also parrot the line back ("You don't think anything.
I'm here to help."), which the eval does not check yet.

**Guards against false senses and templates (2026-10-20, robot paused).** The changes:
- A sense guard. A reply claiming a sense the body did not declare (narrow first-person patterns; "I see
  what you mean" passes) is journaled `rejected` and retried once with a note. If it claims again, only
  the false sentence is replaced ("I can't see yet; I can only hear you."). A reply that opens like a
  claim is held while streaming.
- A closing 4-word template now counts as a repeat.
- Delivery drops sentences said in the last 3 replies, unless nothing would remain.
- A guard's retry quotes the reply it replaces and samples at 0.9 instead of 0.4. Retries at 0.4 often
  returned the very reply they replaced. The temperature is recorded in the retry's prompt.

`make eval`, 7 lines, all checks:

| Build | runs | passing | first piece p50 s | emote | restating (flag only) |
|---|---|---|---|---|---|
| sense guard + closing templates | 2 (Docker) | 0/2 (shared opening) | 0.32–0.36 | 7/7 | 4/7 |
| + quoted repeat note | 5 (host) | 2/5 | 0.32–0.37 | 7/7 | 4–5/7 |
| + drop recently said sentences | 3 (host) | 0/3 | 0.32–0.35 | 7/7 | 2–6/7 |
| **+ retries at 0.9 (now built)** | 3 host + 4 Docker | **3/3 host; Docker 3/4, the last 2 in a row** | 0.27–0.35 (one Docker run 1.00) | 7/7 | 5–6/7 |

No run of the final build claimed sight. "What do you see?" got "I see nothing." or "I don't see anything."
That reply streams nothing, because it opens like a claim and is short, so it arrives whole. Most replies
now restate the person's line ("You don't think anything."): the next thing to fix, flagged and not failed.

## Voice services in Docker vs native (`services.py`, 2026-10-20)

**Servers:** the robot-side voice services (STT and TTS, kept with the Reachy device). The same `app.py` runs both ways:
- **Docker:** Colima (Linux VM, 4 CPUs, 5.8 GiB). Each container gets `cpus: 4`, a read-only
  root, cap_drop ALL, and 2 GiB (STT) or 1 GiB (TTS). Published on 127.0.0.1:8781 and :8782.
- **Native:** uvicorn on 127.0.0.1:8791 and :8792, using all 12 cores of the Mac.

Both are measured over HTTP the way Pipecat calls them:
- **STT:** the 100 clean synthetic clips, posted as WAV multipart.
- **TTS:** 8 replies × 3, `pcm`. TTFB = first body byte, which is the first sentence.

There were three alternating rounds. The machine was loaded throughout (load average about
10, from other work and the running pet), so each cell gives the range across rounds.

| Stage | Docker p50 | Docker p95 | Native p50 | Native p95 | Docker cost at p50 |
|---|---|---|---|---|---|
| STT Parakeet int8 (2.2–4.1 s clips) | 0.168–0.182 | 0.33–0.55 | 0.085–0.107 | 0.11–0.55 | **+70–90 ms (about 1.8×)** |
| TTS Kokoro int8, first audio | 0.214–0.236 | 0.34–0.43 | 0.190–0.215 | 0.29–0.56 | +10–25 ms |
| TTS Kokoro int8, whole reply | 0.50–0.54 | | 0.40–0.54 | | small |
| WER (STT, clean) | 0.44% | | 0.53% | | different onnxruntime builds; noise |

**Reading:** Docker costs about **+0.1 s at p50 on voice-to-voice** (STT plus TTS first audio).
That is most of the margin `qwen3:1.7b` has against p50 ≤ 1 s with a 450 ms end-of-turn wait.
Parakeet is the stage that loses: it uses all the CPU it gets, and the VM has 4 cores to the
Mac's 12. Kokoro barely changes. I have not run native with 4 threads, so this does not
separate core count from virtualisation overhead. **Owner's call whether these run
natively.** A middle way is STT native and TTS in Docker, since TTS loses only about 15 ms.
Resident memory in Docker: STT about 1.0 GiB, TTS about 350 MiB (both sessions).

**Captioned speech** (`POST /dev/captioned_speech`, Kokoro-FastAPI's shape) returns each
sentence's audio with `[{word, start_time, end_time}]`. It runs the timestamped export of the
same int8 weights; its plain-path audio is bit-identical to the plain export. Plain and
captioned were interleaved 40 times, comparing the plain endpoint's first byte with the
captioned endpoint's first line:

| Where | plain TTFB p50 | captioned first line p50 | paired diff p50 | paired diff p95 |
|---|---|---|---|---|
| native | 0.211 s | 0.211 s | **−0.9 ms** | 110 ms (load noise) |
| Docker | 0.226 s | 0.229 s | **−2.5 ms** | 267 ms (load noise) |

In-process, the `durations` output plus per-word phonemisation for alignment cost 1.3 ms
(paired p50, n = 80) and 0.2 ms respectively. That is well under the 30 ms bar, so it ships.

## Recommendation (2026-10-20)

- **Voice path: Parakeet TDT 0.6B v2 int8, then Kokoro 82M int8 (`model_quantized.onnx`),
  one clause at a time.** Parakeet is 4× faster than the pet's whisper `base.en`
  (p50 0.075 s vs 0.31 s) and at least as accurate. Kokoro int8 is 2× faster than fp32. It
  must be fed one clause at a time, because `create_stream` sends a short reply as a single
  batch.
- **Live-turn model: `qwen3:1.7b`, `think:false`, behind the `format_best` harness** (below).
  The chained compute is **0.40 s p50 / 0.53 s p95**. That leaves room for a 450 ms
  end-of-turn wait and still meets the 1 s p50.
- **`qwen2.5:7b` (the owner's "7B-class" pick) passes p95 but misses p50 ≤ 1 s** unless the
  end-of-turn wait drops to about 150 ms (budget below). It chooses emotions better (20/24 fair
  vs 17/24). It fits background work, or a second pass where latency doesn't matter.
  **Owner's call: see the questions at the end.**
- **No live router model.** None of the four models judges "addressed to me?" well enough to
  gate a turn (best 36/40, at 0.44 s). Use deterministic signals instead (its name, the face
  tracker's gaze), and keep the LLM router for background use or ambiguous cases.

## Latency budget (voice-to-voice)

The compute row is measured as a chain (`e2e.py`): clip → Parakeet → Ollama streaming
`format_best` → Kokoro int8 on the first complete clause → audio ready. The clock runs over 20
clips of 2.2–4.1 s (bf_emma), with warm models. The other rows are not measured on this
machine, and the table says so.

| Stage | qwen3:1.7b p50 / p95 | qwen2.5:7b p50 / p95 | Source |
|---|---|---|---|
| End-of-turn wait (VAD silence) | 0.45 / 0.45 | 0.45 / 0.45 | **design choice**: an earlier Pipecat voice bot's calibrated 450 ms (Pipecat default 200 ms + a 600 ms turn wait) |
| STT, Parakeet int8 | 0.068 / 0.075 | 0.067 / 0.073 | measured, e2e |
| LLM, first clause complete | 0.161 / 0.226 | 0.475 / 0.663 | measured, e2e |
| TTS, first clause audio, Kokoro int8 | 0.166 / 0.223 | 0.173 / 0.232 | measured, e2e |
| **Compute subtotal** | **0.395 / 0.526** | **0.710 / 0.946** | measured as one chain, not summed |
| Transport + playback to Reachy (WebRTC, jitter buffer) | ~0.10–0.20 | ~0.10–0.20 | **not measured**: an estimate, to be measured on the robot |
| **Voice-to-voice, 450 ms wait** | **≈ 0.95–1.05 / 1.1–1.2** | **≈ 1.26–1.36 / 1.5–1.6** | |
| **Voice-to-voice, 200 ms wait** | **≈ 0.70–0.80 / 0.85–0.95** | **≈ 1.01–1.11 / 1.25–1.35** | |

Reading it: with either wait, both models meet p95 ≤ 2 s. For p50 ≤ 1 s, `qwen3:1.7b` is just
at the edge with a 450 ms wait and clearly inside with 200 ms. `qwen2.5:7b` is about 0.1 s over
even at 200 ms. Three levers, measured but not yet applied to the budget:
- Piper jenny instead of Kokoro saves about 0.15–0.18 s of first audio (TTFA 0.03 s vs 0.21 s).
  The voice is part of the character, and Kokoro is the owner's pick.
- Every miss of the Ollama prompt cache, or a model reload, costs seconds. `warm_s` was 2–8 s,
  and the router's first call after an unload took 1.97 s. Keep the live model pinned with
  `keep_alive`.
- The pet and the bench share one GPU. A background job on Ollama during a live turn will show
  up at p95. This was not measured here.

## LLM: Ollama native `/api/chat` (`llm.py`, 2026-10-20)

There are 8 pet cases (copied from the current pet's `bench.py`), each run 3 times, so N = 24
per cell. Settings: temperature 0.7, `num_predict` 200, streamed, and `think:false` for qwen3.
- **valid**: the output parses, the emotion is one of the 30 `EMOTIONS`, and the reply is
  non-empty.
- **leaks**: the reply contains a label (`cheerful1`, `EMOTION:`), an emoji, a `*stage
  direction*`, or a brace.
- **fair**: the emotion is in the case's acceptable set.
- **speak**: the first speakable character of the reply exists.
- **clause**: the reply's first clause is complete. Tool calls arrive whole, so for tools,
  speak = clause = total.

Modes:
- `prose`: the pet's current `EMOTION:` line, then free text, parsed strictly.
- `tools`: an `emote(name: enum[30], reply)` tool.
- `tools2`: the same tool, with the argument renamed to `emotion`.
- `format`: a JSON schema `{emotion, reply}`.
- `format_rf`: the same schema, reply first.
- `format_gloss`: `format` plus a short gloss per emotion in the prompt.
- **`format_best`**: reply first, the glosses, and a grammar `pattern` on the reply,
  `^[A-Za-z ,.!?'-]+$`.

| Model | Mode | valid/24 | leaks | fair/24 | speak p50 | clause p50 / p95 | total p50 / p95 |
|---|---|---|---|---|---|---|---|
| qwen2.5:7b | prose | 5 | 16 | 5 | 0.42 | 0.47 / 0.90 | 0.55 / 0.90 |
| qwen2.5:7b | tools | 7 | 0 | 4 | 1.07 | 1.07 / 1.98 | 1.07 / 1.98 |
| qwen2.5:7b | tools2 | 23 | 1 | 14 | 1.21 | 1.21 / 1.44 | 1.21 / 1.44 |
| qwen2.5:7b | format | 24 | 0 | 13 | 0.44 | 0.54 / 0.65 | 0.84 / 0.97 |
| qwen2.5:7b | format_rf | 24 | 0 | 12 | 0.20 | 0.30 / 0.43 | 0.79 / 0.98 |
| qwen2.5:7b | format_gloss | 24 | 0 | 17 | 0.38 | 0.51 / 0.68 | 0.81 / 1.07 |
| qwen2.5:7b | **format_best** | **24** | **0** | **20** | 0.26 | 0.37 / 0.50 | 0.79 / 1.17 |
| qwen3:1.7b | prose | 3 | 18 | 3 | 0.14 | 0.16 / 0.27 | 0.21 / 0.28 |
| qwen3:1.7b | tools | 0 | 21 | 0 | 0.45 | 0.45 / 0.74 | 0.45 / 0.74 |
| qwen3:1.7b | tools2 | 0 | 21 | 0 | 0.52 | 0.52 / 0.82 | 0.52 / 0.82 |
| qwen3:1.7b | format | 24 | 0 | 9 | 0.22 | 0.25 / 0.37 | 0.50 / 0.68 |
| qwen3:1.7b | format_rf | 24 | 2 | 14 | 0.23 | 0.27 / 0.31 | 0.90 / 1.40 |
| qwen3:1.7b | format_gloss | 24 | 0 | 16 | 0.32 | 0.43 / 0.66 | 0.67 / 0.99 |
| qwen3:1.7b | **format_best** | **24** | **0** | **17** | 0.15 | 0.20 / 0.31 | 0.68 / 0.90 |
| gemma3:1b | prose | 0 | 16 | 0 | 0.13 | 0.13 / 0.25 | 0.13 / 0.25 |
| gemma3:1b | tools, tools2 | Ollama 400: "does not support tools" | | | | | |
| gemma3:1b | format | 24 | 1 | 8 | 0.43 | 0.47 / 0.55 | 0.68 / 0.99 |
| gemma3:1b | format_rf | 22 | 3 | 10 | 0.28 | 0.31 / 0.40 | 1.19 / 6.38 |
| gemma3:1b | format_gloss | 24 | 2 | 12 | 0.48 | 0.54 / 0.68 | 0.89 / 1.27 |
| gemma3:1b | **format_best** | **24** | **0** | 13 | 0.36 | 0.41 / 0.54 | 1.30 / 1.68 |
| qwen3:4b | prose | 0 | 23 | 0 | 4.79 | 4.85 / 5.04 | 4.85 / 5.04 |
| qwen3:4b | tools | 0 | 20 | 0 | 5.12 | 5.12 / 5.34 | 5.12 / 5.34 |
| qwen3:4b | tools2 | 0 | 20 | 0 | 5.08 | 5.08 / 5.43 | 5.08 / 5.43 |
| qwen3:4b | format | 24 | 1 | 14 | 0.45 | 0.55 / 0.78 | 0.85 / 1.05 |
| qwen3:4b | format_rf | 24 | 2 | 10 | 0.26 | 0.40 / 0.58 | 1.06 / 1.47 |
| qwen3:4b | format_gloss | 24 | 0 | 18 | 0.43 | 0.52 / 0.70 | 0.81 / 1.09 |
| qwen3:4b | **format_best** | **24** | **0** | **18** | 0.26 | 0.37 / 0.48 | 0.91 / 1.17 |

Time to first token (any chunk) was 0.02–0.10 s for every model and mode, except where noted
in the findings. It is not what the user hears, so the table shows `speak` and `clause`.

What the harness bought, and what it didn't:
- **`format` (grammar-constrained JSON) is the reliable channel.** All four models scored 24/24
  valid, against 0–5/24 for the same contract in prose. A 1B model can't emit a malformed
  action through it.
- **Native tool calls are the weakest channel on this Ollama.**
  - With the argument called `name`, `qwen2.5:7b` writes `{"name": "welcoming1", ...}`, putting
    the emotion where the function name goes. Ollama silently drops calls to unknown functions
    and returns empty content: 7/24 valid, which `/api/generate raw` confirmed. Renaming the
    argument to `emotion` gives 23/24.
  - qwen3 emits text pseudo-calls (`<tool>…`) or reasoning: 0/24.
  - gemma3:1b has no tools template.
  - Tool calls also arrive whole, so speech cannot start until the end: +0.8 s at p50 against
    `format_best`.
- **qwen3 with `think:false`.** `qwen3:1.7b` obeys: no thinking, and no reasoning in content.
  The installed `qwen3:4b` (the 256k-context build) ignores it in prose and tools. It writes
  about 145 words of "We are given…" into content, taking about 5 s. Only `format` stops it.
- **Reply first** halves speak time, because the emotion lands about 5 tokens after the reply.
  Alone, it lets small models run on, with p95 totals of 1.4–6.4 s and emoji tails.
- **Glosses** (`format_gloss`) are the main gain in emotion fit: 7b went from 13 to 17/24 fair,
  1.7b from 9 to 16, 4b from 14 to 18.
- **A `pattern` on the reply** (`format_best`) makes emoji, markdown and labels impossible to
  decode. It brought leaks to 0 everywhere and kept runaways short.
- **Costs found in `format_best`:**
  1. No digits means numbers must be spelled out. Once, `qwen3:4b` turned a transcript's "at 6"
     into "at sixty" (e2e). Fix: normalise digits to words in the transcript before the turn,
     or allow digits and strip `\w+1` labels after decoding.
  2. Once, 7b replied with the bare word "serenity", a label without its digit, which the
     regex can't catch.
  3. `qwen3:1.7b` sometimes repeats itself ("I'm so glad…", three times). Keep `num_predict`
     tight, and cut at the spoken budget.
- `fair` is graded against hand-written acceptable sets on 8 lines. It is a coarse signal. A
  human ear is still needed for tone.

### Router helper: "Is this addressed to the robot?" (`llm.py`, 2026-10-20)

There are 20 **synthetic** lines, written for this bench (10 yes, 10 no), each run twice
(N = 40). Settings: `format` = `{addressed: yes|no}`, temperature 0, non-streamed. Latency is
the full call. `router_fewshot` adds six worked examples, none of them test lines. Both the
examples and the test lines were written by the same author, so that row is optimistic.

| Model | router correct/40 | p50 / p95 s | router_fewshot correct/40 | p50 / p95 s |
|---|---|---|---|---|
| qwen2.5:7b | 30 | 0.373 / 0.386 | 32 | 0.384 / 0.390 |
| qwen3:1.7b | 24 | 0.231 / 0.265 | 24 | 0.226 / 0.258 |
| gemma3:1b | 24 | 0.360 / 0.433 | 28 | 0.416 / 0.461 |
| qwen3:4b | 32 | 0.436 / 0.441 | 36 | 0.444 / 0.467 |

The small models call unnamed requests "no", for example "Tell me a joke." and "Set a timer…",
while the 4b calls family-addressed lines "yes", for example "Honey, did you feed the cat?".
**None of them is fit to gate a live turn**, and even the fastest adds 0.23 s in series.

## STT (`stt.py`, 2026-10-20)

There are 20 pet-style lines, each **synthesised** by four Kokoro voices (af_heart, bf_emma,
am_michael, bm_george) and by piper jenny. That gives 100 clips of 2.2–4.1 s (p50 3.2 s), each
run clean and with white noise at 15 dB SNR (N = 200 per model). Latency is one `recognize` /
`transcribe` call on 16 kHz audio, warm, CPU. RTF = latency / clip length.

Whisper ran with the pet's own settings (`live.py`): beam 1, temperature 0, VAD filter. Before
scoring, punctuation was removed, case was folded, and spelling variants (mom/mum,
good night/goodnight, 6/six, 25/twenty five) were counted as the same word.

| Model | p50 s | p95 s | RTF p50 | WER clean | WER 15 dB | WER all |
|---|---|---|---|---|---|---|
| **Parakeet TDT 0.6B v2 int8** (onnx-asr, as an earlier Pipecat voice bot runs it) | **0.075** | **0.098** | **0.023** | 0.44% | 0.44% | 0.44% |
| faster-whisper base.en int8 (the pet today) | 0.305 | 0.330 | 0.096 | 0.53% | 0.62% | 0.58% |
| faster-whisper small.en int8 | 1.023 | 1.094 | 0.323 | 0.44% | 0.53% | 0.49% |

Synthetic speech is far cleaner than a desk microphone, so these WERs are a floor. They do not
predict what the robot will hear, and the WER gaps are too small to rank models. The speed gap
is real.
- Every model heard "commutator" as "car mutator" in at least one voice. The pet's
  "F-mer commutator" problem is not fixed by changing models; a vocabulary or context hint is
  the lever.
- An earlier run on 1.6–2.7 s clips gave a similar ratio: Parakeet 0.085 / 0.117 s, base.en
  0.355 / 0.443 s.

## TTS (`tts.py`, 2026-10-20)

There are 8 pet replies of 1–2 sentences (1.4–3.4 s of audio), each run 3 times (N = 24),
warm, CPU.
- **TTFA** (time to first audio) runs from text in to the first audio chunk out. For Kokoro
  that is the first item of `create_stream`; for piper it is the first sentence chunk.
- **RTF** = synthesis time / audio length.

| Engine | TTFA p50 s | TTFA p95 s | RTF p50 | RTF p95 |
|---|---|---|---|---|
| Kokoro 82M int8 (`onnx/model_quantized.onnx`), whole reply | 0.379 | 0.613 | 0.161 | 0.225 |
| **Kokoro 82M int8, one sentence at a time** | **0.214** | **0.381** | 0.178 | 0.205 |
| Kokoro 82M fp32 (`onnx/model.onnx`), whole reply | 0.739 | 0.927 | 0.267 | 0.333 |
| piper `en_GB-jenny_dioco-medium` | 0.032 | 0.068 | 0.031 | 0.033 |

- The weights come from Hugging Face, `onnx-community/Kokoro-82M-v1.0-ONNX`. Its per-voice
  `.bin` files are packed into the npz that kokoro-onnx expects (`tts.kokoro()`). Voice
  `af_heart`.
- I tried the CoreML execution provider on int8. It took about 0.59 s per reply, no faster
  than CPU, and only 1304 of 3395 nodes ran on CoreML, so it is not worth it.
- An earlier run had Kokoro int8 TTFA at p50 0.486 s for the whole reply, which shows the
  run-to-run noise under load.
- The quality of the two voices was not judged here; it needs a human ear.

## Voice first audio: where the first clause is cut (2026-10-20)

From "Generating TTS" (the first `run_tts` of a reply) to "Bot started speaking" (the
first audio at the output transport). The 8 replies of live run 3 (`voice-live-run-3.log`)
were replayed 5 times each (N = 40 per variant, per run). The path was the voice app's own
`KokoroTTSService` (captioned speech) and `ReachyOutputTransport`, with a fake robot and
pacing off, against the live Kokoro container on 127.0.0.1:8782. There was no LLM and no
kernel: each reply's tokens were fed at once, so the gap is TTS plus transport only. The
measurement was taken twice, because the first run had a slow tail.

| First chunk sent to Kokoro | Run 1 p50 / p95 ms | Run 2 p50 / p95 ms |
|---|---|---|
| Clauses of 4+ words (current default) | 430 / 2559 | 326 / 640 |
| **First clause of 2+ words, then 4+** | **313 / 570** | **319 / 537** |
| First 3 words whatever the punctuation (a stand-in for streaming within a sentence) | 233 / 381 | 259 / 637 |

- The services' Kokoro synthesises a whole request before sending audio, so true streaming
  within a sentence is not available without a server change. The third row cuts the first
  chunk after 3 words instead, which bounds what such streaming could gain.
- Only 3 of the 8 replies have an early comma ("Yes, that's true,"; "Great, let's…"; "Yes,
  I…"), so the 2-word rule changes few replies. Its tail is the most stable across runs.
- Live run 3 measured 0.41–0.73 s for the same gap. The live robot run shared the CPU with
  the kernel and Ollama, which this replay did not.
- **Recommendation:** a 2-word first clause. It is a one-constant change, never cuts inside
  a phrase, and has the steadiest p95. Cutting after 3 words regardless of punctuation is
  faster at p50 (about 70 ms) but breaks phrases mid-flow: judge it by ear on the robot
  before adopting it. The default is unchanged; the lead and the owner decide.

## Questions for the owner

1. **Live model.** `qwen3:1.7b` meets the target with margin. `qwen2.5:7b` chooses emotions
   better and misses p50 ≤ 1 s by about 0.1–0.3 s. Choose either 1.7b live with 7b in the
   background, or 7b live with a ≤ 150 ms end-of-turn wait (which will split more turns).
2. **End-of-turn wait.** 450 ms (an earlier Pipecat voice bot, calibrated on real speech) or 200 ms (Pipecat's
   default)? This dominates the budget.
3. **Voice.** Kokoro, as decided, or piper jenny (the current character, about 0.18 s sooner)?
