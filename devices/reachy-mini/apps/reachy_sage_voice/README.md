---
title: Reachy Sage Voice
emoji: 🎙️
colorFrom: red
colorTo: blue
sdk: static
pinned: false
short_description: Local real-time voice for Reachy Mini, Sage as its mind
license: apache-2.0
tags:
 - reachy_mini
---

# Reachy Sage Voice

Real-time voice for Reachy Mini, with a [Sage kernel](https://github.com/1xabhay/sage-kernel) (v0.2.0)
as its mind. It runs on [Pipecat](https://github.com/pipecat-ai/pipecat): mic,
barge-in on speech onset, Parakeet speech to text, the kernel, Pocket TTS speech
one clause at a time, then the speaker, with the robot's recorded moves as
emotes. Every model runs on your own machine.

**Install from source (`uv sync`); dashboard install is pending: see Known limits.**

## Quick start

Prerequisites:

- macOS or Linux, with [uv](https://docs.astral.sh/uv/) and Docker (Compose v2).
- A Reachy Mini (Lite or Wireless) with its daemon running, on your network.
- [Ollama](https://ollama.com) with the kernel's model:
  `ollama pull qwen3:4b-instruct` (Qwen3-4B-Instruct-2507, the live model).
- A Sage kernel ([github.com/1xabhay/sage-kernel](https://github.com/1xabhay/sage-kernel))
  running its device listener (protocol v1), with a token for this robot in its
  `SAGE_DEVICES`. Its README covers both.

Then, from this folder:

```sh
uv sync                  # this app's own .venv
make -C services up      # STT on 127.0.0.1:8781, TTS on 127.0.0.1:8782 (Docker)

cp .env.example .env     # then set SAGE_VOICE_KERNEL_TOKEN, this robot's token from the kernel
export REACHY_MINI_HOST=reachy-mini.local     # the robot's daemon (or set it in .env)
make run
```

Stop anything else driving the robot first. The run checks every setting and
that the kernel, STT and TTS answer, then enables the motors, wakes the robot
and talks until Ctrl-C. The first services build downloads the models into
the images; after that the services run offline.

`make status` says up or down, and why, for the robot's daemon, the kernel,
STT, TTS, research and Ollama, and, while the voice runs, its motion link. If the
robot's link drops mid-run (its mic goes quiet for 5 s, a move finds the motion
link gone, or the SDK refuses every command for 10 s), `make run` logs an ERROR
and exits 1 rather than sitting deaf. Short motion-link stalls are logged once
each ("motion link down" / "motion link back").

Moves by voice: "Sage, show me happy" (or be surprised, look sad, act curious...)
plays a recorded emotion; "do your groovy dance" or "do a dance move" plays one of
Pollen's recorded dances; "what moves can you do?" lists them.

## Where it runs

`make run` runs on your computer and reaches the robot over the network, for
both Lite and Wireless. The STT and TTS services listen on 127.0.0.1 only, so
the app must run on the same machine as them.

The app also has the standard `reachy_mini_apps` entry point
(`reachy_sage_voice.main:ReachySageVoice`), so a Reachy Mini daemon can launch
it. Launched that way, it runs wherever the daemon runs (your computer for a
Lite, the robot's Raspberry Pi for a Wireless). It inherits only the daemon's
environment, so put its settings in `~/.config/reachy_mini/sage_voice/.env`
(see "Settings"), and the daemon connects to the robot, so `REACHY_MINI_HOST`
is not needed. Installing it from the dashboard does not work yet, so the Space
is not tagged `reachy_mini_python_app` and the dashboard does not offer it: see
"Known limits". Running on the Pi is not a goal for 0.1.0.

## Deploy

On the owner's Mac the voice is a launchd LaunchAgent, not a terminal: launchd
restarts it when it exits (`KeepAlive`, at most every 10 s). It runs a
`git archive` export of one commit, never a working tree:

```sh
make env                 # once: writes the .env below from .env.example, mode 600; never overwrites
                         # then fill in SAGE_VOICE_KERNEL_TOKEN and REACHY_MINI_HOST
make deploy              # HEAD; or make deploy SHA=<commit>, which is also the rollback
make deploy RECORDINGS=<folder>  # also record each start's utterances in <folder>/session-<time>
make logs                # follow the running voice's log
make agents              # the voice's and azelma's LaunchAgents: loaded, pid, last exit
make undeploy            # stop it and remove the LaunchAgent
make -C services pocket-on   # azelma natively as its own LaunchAgent; pocket-off back to Docker
```

A lost robot link never ends the voice: it waits for the robot, a log line a
minute, and carries on when the robot is back, beam routed again.

`make deploy` refuses a dirty tree, exports the commit to `.deploy/voice/<sha>`
at the monorepo root (git-ignored; the last three stay), runs `uv sync --locked`
and the whole test suite there, then `make status`, and only then loads
`~/Library/LaunchAgents/ai.sage.reachy-voice.plist` and waits for the log
(`~/Library/Logs/reachy-sage/bot-<sha>.log`) to show the pipeline running. Each
deploy adds a line to `~/Library/Logs/reachy-sage/DEPLOYS.log`. One known
failing test can be let through, loudly, with `DEPLOY_ALLOW_XFAIL=<nodeid>`. The
settings come only from the `.env` in the reachy_mini config directory (below):
the plist holds no settings and no token. Stop a hand-run `make run` first: the
deploy refuses while one runs. Both hold the Mac awake with `caffeinate`, but a
closed lid on battery sleeps anyway: leave it open, on its charger.

The Hugging Face Space is distribution only. `make release` uploads HEAD when
it is tagged `voice/v<version in pyproject.toml>`: a whitelist of the tracked
files (the app, this README, the services without their tests or the owner's
voiceprint), scanned for addresses and home folders first. `DRY_RUN=1 make
release` lists the files and stops. The wheel carries `sage_device` inside it,
so a plain `pip install` needs no path dependency.

## Deploy and maintain

The Docker services (STT, research, SearXNG) deploy like the kernel: one image
per commit, `reachy-sage-<service>:<sha>`, built from `git archive` of that
commit. The voiceprint volume `reachy-sage-voice-profiles` is never touched.

```sh
make -C services deploy SHA=<sha>              # build, contract tests, up --wait, health; else back to the running tag
make -C services up-tag SAGE_SERVICES_TAG=<sha> # rollback: any tag a deploy built (grep services DEPLOYS.log)
make -C ../../../.. prune                      # old Sage image tags, voice exports, week-old recordings, journal backups
DRY_RUN=1 make -C ../../../.. prune            # the same, listed only
make -C ../../../.. maintain-on                # nightly 04:15: make backup, then make prune (ai.sage.maintain)
make -C ../../../.. maintain-off               # unload and remove it
```

`make prune` keeps the newest 3 tags of each `sage-kernel` and `reachy-sage-*`
image and any a container uses, the newest 2 voice exports and the running one,
a week of recording sessions, and the newest 14 journal backups. It never
touches a volume or any other image. Each deploy, rollback and up-tag adds a
line to `~/Library/Logs/reachy-sage/DEPLOYS.log`.

## Settings

Every setting is listed, with its default, in [`.env.example`](.env.example).
Copy it to `.env` and fill it in, as for Pollen's conversation app. The app
reads, first one wins:

1. the environment;
2. `.env` in the reachy_mini config directory, beside the daemon's own config:
   `~/.config/reachy_mini/sage_voice/.env` on Linux
   (`~/Library/Application Support/reachy_mini/sage_voice/.env` on macOS). Use
   this one when the daemon launches the app;
3. `.env` in this folder (for `make run`). Never one above it: the monorepo root's `.env` is the kernel's.

`.env` is gitignored: never commit a token. Addresses and the token have no
defaults, so nothing reaches the network unless you configure it to, and a
missing one stops the app with its name.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SAGE_VOICE_KERNEL_URL` | required | The kernel's device listener (protocol v1) |
| `SAGE_VOICE_KERNEL_TOKEN` | required | This robot's token from the kernel's `SAGE_DEVICES` |
| `SAGE_VOICE_STT_URL` | required | Parakeet, OpenAI-compatible, with or without `/v1` |
| `SAGE_VOICE_TTS_URL` | required | Pocket TTS, OpenAI-compatible |
| `REACHY_MINI_HOST` | required for `make run` | The robot's daemon |
| `SAGE_VOICE_STT_MODEL` | `parakeet-tdt-0.6b-v2` | Model name sent to the STT |
| `SAGE_VOICE_TTS_MODEL` | `kokoro` | Model name sent to the TTS |
| `SAGE_VOICE_TTS_VOICE` | `azelma` | Any voice the TTS lists (`GET /v1/audio/voices`) |
| `SAGE_VOICE_TURN` | `silence` | `silence`: a pause ends your turn. `smart`: Smart Turn v3 decides, and the STT hears the turn whole |
| `SAGE_VOICE_TURN_END_MS` | `600` | Silence that ends a turn. Measured on the owner's live turns (`eval/turn_taking.py`, `eval/LEDGER.md (turn-taking rows)`); each 100 ms more is 100 ms on every reply |
| `SAGE_VOICE_HOLD_MS` | `0` (off) | A turn whose transcript stops mid-sentence (no closing `.?!`, or ends on "and", "uh"...) waits this long in all from the end of speech, so the rest joins it; e.g. `2000` |
| `SAGE_VOICE_BARGE_IN_MS` | `200` | Speech that must last before the robot stops talking; `0` stops it on the VAD's first voiced frame (breaths, knocks and its own voice did, live) |
| `SAGE_VOICE_TURN_TIMEOUT_MS` | `1500` | `smart` only: silence that ends a turn Smart Turn calls incomplete |
| `SAGE_VOICE_MIC_CHANNEL` | `mean` | `mean`, `0` or `1` of the mic array's stereo |
| `SAGE_VOICE_PRESENCE` | `0` | `1`: follow a face from the start, and report seen/lost from it |
| `SAGE_VOICE_WATCH` | `1` | Presence without following: the face detector runs at a weight that never moves the head, and seen/lost (left after 2 min unseen) go to the kernel for greetings and nudges. `0`: detector off |
| `SAGE_VOICE_IDLE` | `1` | `0`: no idle motion and no glance toward your voice when you start speaking |
| `SAGE_VOICE_IDLE_SCALE` | `1` | Multiplies every idle amplitude (head about 12° yaw, 6° pitch, 4° roll, body sway 8°, antennas), within fixed limits: `0.5` calmer, `1.5` livelier, `0` still |
| `SAGE_VOICE_MOTION_SPEED` | `1` | With the motion arbiter (the default; `SAGE_VOICE_ARBITER=0` rolls back to the old path): how fast moves go (looks, turns, glances, home; at `1` the body turns at most 45°/s, the head 90°/s), from `0.25` to `2`: `0.5` slower and softer, `2` twice as fast |
| `SAGE_VOICE_LISTEN_CUES` | `1` | `0`: no thinking glance between your turn and the reply |
| `SAGE_VOICE_ADDRESSED` | `0` | `1`: answer only when addressed: a face is tracked, its name is said, or it spoke in the last few seconds. Other speech (lyrics, TV) is not sent |
| `SAGE_VOICE_ALIASES` | `Reachy` | Names besides Sage it answers to, comma-separated |
| `SAGE_VOICE_EXCHANGE_S` | `8` | Seconds after it spoke during which no name or face is needed |
| `SAGE_VOICE_RECORD_DIR` | off | A local folder: save each utterance as a WAV |
| `SAGE_VOICE_NAMES` | none | Names to hear and say right, `written:spoken:heard\|heard`, comma-separated: e.g. `Mika:Mee-kah:Micah\|Mica` corrects the STT's mishearings before the kernel and has the voice say it as respelled |
| `SAGE_VOICE_RESEARCH_URL` | off | The song research service (`http://127.0.0.1:8783`): Sage can listen to a song and answer questions about it. Needs a kernel with notes (protocol §10) |

## Privacy

- Speech to text, speech and the kernel's model all run on your machine. The
  services bind to 127.0.0.1, run in sealed containers and run offline once built.
- Song research is the exception: it uses the internet. `/identify` sends Shazam
  only an audio fingerprint, never the audio. `/research` sends the song's title
  and artist to MusicBrainz, Wikipedia, LRCLIB and, through your own SearXNG, the
  search engines it aggregates.
- The app sends audio only to the STT URL and text only to the kernel and TTS
  URLs you set.
- Recordings are off by default. `SAGE_VOICE_RECORD_DIR` saves your voice to
  that folder: keep it local and never commit it.
- The robot's recorded moves come from Pollen's public dataset, read from the
  local Hugging Face cache (downloaded once if it is missing).

## Song research

`services/research` names a song from a short mic snippet and gathers what is
findable about it, so Sage can answer questions about what is playing: its
history, credits, the artist, the artist's news, style and lyrics. No model
runs in it, hosted or local.

```sh
make -C services up-research      # research on 127.0.0.1:8783, and its private SearXNG
make -C services research-smoke   # one real lookup: "Blinding Lights" by The Weeknd
```

- `POST /identify`, multipart `file`: a WAV (16 kHz mono, about 10 s). Shazam,
  through shazamio, answers `{"found": false}` or `{"found": true, "title",
  "artist", "album", "year", "genre"}`.
- `POST /research`, JSON `{"title", "artist"}`: `{"topic", "passages"}`, at most
  64 passages of at most 600 characters, most useful first, each tagged with its
  source (`MusicBrainz: ...`, `Wikipedia, the song: ...`, `Lyrics (LRCLIB): ...`,
  `News, <date>, <site>: ...`, `Web, <site>: ...`). Passage 0 is a card: title,
  artist, featuring, album, release date, genre, length, label. The sources run
  at once within 20 s; one that fails or is slow is left out.
- SearXNG has no host port and a fresh secret at every start
  (`services/searxng/settings.yml`). The two share their own network, apart
  from STT and TTS.

With `SAGE_VOICE_RESEARCH_URL` set, say "Sage, listen to this song". Sage says
it is listening, takes 10 s of the music, says what it is, reads up on it (about
10 s), and says when it is ready. Then ask anything: who produced it, what the
chorus says, what the artist is up to. The passages go to the kernel as notes,
and each answer is built from the three that best match your question. While
Sage listens and names the song, nothing it hears is sent and the music does
not cut it off. A new song replaces the last one's notes.

Naming under simulated room damage (`make bench-identify`, 5 iTunes previews,
2026-10-22): 95 of 95 found, median 0.2-0.5 s, through pink noise and babble
down to 0 dB SNR, 20 dB down, 0.6 s reverb, 1.5 s of speech at the start, and
all of these at once at 5, 8, 10 and 12 s. Where it breaks (1.2 s reverb, fan
and talkers both at 0 dB, 20 dB down): 0/5 at 5 s, 4/5 at 10 s, 0/5 at -5 dB;
babble at -10 dB 2/5. Shazam fingerprints the centred 10 s: a 12 s fingerprint
did no better (3/5) and 15 s worse (0/5), so 10 s stays. Not yet measured on
the robot's own mic.

## Only your voice

The STT can hear only you. Record some of your turns, keep only your own, and
enroll them:

```sh
SAGE_VOICE_RECORD_DIR=services/profiles/recordings make run   # talk for a minute, then Ctrl-C
make -C services enroll
```

The recordings stay in `services/profiles/` (git-ignored) and go to the STT as
a tar stream: nothing is mounted from the host. `enroll` prints each recording's similarity to the voiceprint it made (a low
one may be someone else: delete it and enroll again) and restarts the STT.
From then on, speech in any other voice, and music, comes back as no text, so
it never starts a turn. Barge-in waits 0.8 s and checks your voice too, so a
song or another person cannot cut Sage off; you can, a little later than
before. `SAGE_SPEAKER_THRESHOLD` (default 0.5) on the STT sets how close counts
as you. The voiceprint is `owner.npy` in the `reachy-sage-voice-profiles`
Docker volume: biometric, local, never committed, kept by `make -C services
down`. `make -C services unenroll` deletes it, and the STT hears everyone.

## With the simulator

The simulator ([`reachy_sim`](https://github.com/1xabhay/sage-kernel/tree/main/devices/reachy-mini/apps/reachy_sim), beside this app) has no mic or
speaker, so it cannot run the voice. It runs the rest without a robot: the kernel's
replies and the recorded moves as emotes, over the same device protocol. Its device
test starts its own headless sim daemon and a throwaway kernel (temporary state, the
conformance suite's fake model), says "Feel happy" and checks the simulated head moved.
It never reaches the live kernel or its journal.

```sh
cd ../reachy_sim && uv run pytest -q tests/test_device.py
```

## Development

```sh
make test                # the app's tests: no robot, no network, no GPU
make -C services test    # the STT and TTS contract tests (loads the real models), and research's (offline)
make hygiene             # lint, imports, vuln (uv audit; audit-ignore.txt says why each is ignored), secrets, test
```

## Known limits

- Dashboard install fails today. The daemon installs an app with
  `uv pip install` into its shared apps environment, and the app's
  `sage-device` comes from this checkout (`[tool.uv.sources]` in
  `pyproject.toml`), which `uv pip install` does not read.
- The voice needs the robot's audio: the simulator cannot stand in for it.

## Licence

Apache-2.0 ([LICENSE](LICENSE)). Third-party models and software keep their own
licences: see [NOTICE](NOTICE).

Voice: [Kyutai Pocket TTS](https://huggingface.co/kyutai/pocket-tts-without-voice-cloning)
([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)); speaker from the
[CSTR VCTK Corpus](https://doi.org/10.7488/ds/2645), University of Edinburgh
([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)).
