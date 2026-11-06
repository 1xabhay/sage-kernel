# Reachy Sage plan (2026-10-22, after the expert panel)

Panel: speech and audio; robotics and embodiment; agent kernel and small LLMs; QA, eval and
reliability. Their reviews are merged here. Rule for every task: a red commit with the failing test
or check first, then green with the whole suite passing; anything that measures adds a dated row,
with n and a confidence interval, to eval/LEDGER.md; anything live passes `make preflight`.

## Decisions taken in the merge
- **The ASR beam is routed before any recording**, and clips are saved in stereo: conference on L,
  ASR beam on R. One session then gives a paired comparison of the two (speech).
- **Commands:** a closed command set handled by a device grammar first (0 ms, exact), and one kernel
  tool `body_plan` for chains and unusual wording. Its steps are flat: a skill, an argument, and
  optionally `until face_found|timeout`. No protocol change. No separate parsing model call, since
  it would evict Ollama's prompt cache (kernel, robotics).
- **"Stop"** is matched on the device before the kernel and always wins: it freezes the head in
  place within 1 s, with motions cut into segments of 0.8 s or less (robotics, kernel).
- **The executor is the head's only owner.** Tracking, emotes, listen cues and plans all go through
  it (robotics).
- **Vision is declared only once a local vision model describes frames on request.** Until then
  the manifest's `body` says the camera is not connected (kernel, robotics).
- **Honest metrics:** at least 2 recording sessions on different days, split dev/test by session,
  the test set frozen by hash, at least 300 impostor clips replayed unattended, and every metric
  reported with its paired metric (name hits with false wakes) (QA, speech).

## Owner decisions (2026-10-22)
1. Protocol: optional manifest `body` (300 characters or less) in the system prompt. **Approved.**
2. Protocol: a `done` result's text reaches the model next turn. **Approved.**
3. Protocol: notes appear only on turns they match. **Approved.**
4. Robot writes: route the ASR beam; restrict UDP to the Mac; disable the Pollen central relay.
   **Approved** (each behind a rollback and verified by `make -C robot check`). Snapshot helper: later.
5. Recording: 2 sessions on different days (about 20 min each) plus unattended impostor replay. **Approved.**
6. Waves 0 and 1 start now as parallel lanes in separate worktrees. **Approved.**

## Wave 0: gates (parallel, now)
| Task | Done when |
|---|---|
| G1 app `make lint vuln secrets hygiene`, tests in Docker | fails on a planted lint error or secret, passes clean |
| G2 pre-commit gate, `(red)` convention | a non-red commit with a failing test is refused |
| G3 find the flaky test | 50-run loop, normal and under load, names it; after the fix 100/100 |
| G4 compose test: loopback ports, sealed services | fails if any port is not on 127.0.0.1 or a service is unsealed |
| G5 `make preflight` | each missing check (status, robot check, journal backup, GPU idle) exits nonzero with a reason |
| G6 tracked-file guard | fails if git tracks *.wav, *.npy or .env |

## Wave 1: build against fakes (parallel)
Eval lane (eval/*):
| Task | Done when |
|---|---|
| E1 stereo recorder + channel map | fake media with L≠R gives a 2-channel WAV and the manifest records the map |
| E2 script extensions | name-dense block, command block, takes with TTS overlap, 10 s song clips; counts asserted |
| E3 paired stats | McNemar and paired-bootstrap CI on a synthetic set with a known answer |
| E4 speaker scorer fix | VAD-trimmed, onset-cropped windows, leave-one-session-out, top-k enrollment |
| E5 addressed scorer | name hits and false wakes per hour, from transcripts run through `Addressed` |
| E6 pipeline-path STT score | replay through VAD + segmented STT; WER against the oracle on a fixture |
| E7 eval split discipline | the speaker report refuses 1 session or 0 impostors; rows carry n, CI and manifest hash |

Commands lane (new files: plan.py, grammar.py, executor.py, skills; sage.py only at wiring):
| Task | Done when |
|---|---|
| C1 plan schema + validator | bad skill, arg or range refused; `turn 180` becomes 160 with text `out_of_range` |
| C2 grammar | 30-line command set maps to the expected plans; chit-chat maps to none |
| C3 executor core | sequential steps on FakeMini; a new plan reports the old one `interrupted` |
| C4 stop wins | stop mid-turn freezes within 1 s and calls cancel_move |
| C5 search_face until found | found on sweep 3 gives done; never found gives failed:not_found; DoA picks the side |
| C6 overlay arbitration | an emote during tracking gives the tracking weight back afterward |
| C7 fragments add up | a fragment plus its continuation makes one kernel submit, not a replacement |

Kernel lane (sage-kernel, one agent owns protocol files; C, D and E queue behind it):
| Task | Done when |
|---|---|
| K1 guard check | "I have a camera" passes; "I see a red shirt" and "I can see you" are rejected |
| K2 `maxItems`/`minItems` in check() | 5 steps against maxItems 4 refused |
| K3 prompt token budget | worst-case compiled prompt is under 3600 tokens |
| K4 `body` field (decision 1) | check_manifest keeps or refuses it; prompt byte-stable; conformance 400 on malformed |
| K5 done-result text (decision 2) | the next prompt holds it; earlier prefix unchanged; conformance |
| K6 notes only when relevant (decision 3) | an unrelated line gets no notes |
| K7 eval fixtures | commands, self-knowledge, notes Q&A; fail on today's build |

Robot lane (robot/):
| Task | Done when |
|---|---|
| R1 health logger additions | sample has power_save, daemon CPU and control-loop stalls; fixtures parse |
| R2 chip route script (decision 4) | apply, verify and revert of the ASR beam route over fake HTTP; `make status` checks it |
| R3 UDP guard (decision 4) | rules allow UDP from the Mac and mDNS, drop the rest; rollback armed |

## Wave 2: serial (robot, GPU or owner, one at a time)
1. Route the beam and set its gain (R2 live): speech peaks −6 to −20 dBFS, 0 clipped samples.
2. CPU profile: daemon threads in 4 states. Decides between audio-only and on-robot snapshot.
3. Impostor replay: 300 clips or more, unattended.
4. Owner session 1, then label check. Owner session 2 on another day: the frozen test set.
5. Baseline LEDGER rows: STT (both channels), speaker, turns, latency n≥30, song Q&A.

## Wave 3: experiments by metric, then wiring
STT bake-off; speaker model and threshold on dev, reported on test; fuzzy aliases scored with false
wakes; turn end vs Smart Turn with wait time; kernel command eval (plan recall ≥ 0.8, false actions
≤ 1/30); wire the executor (C9) and run a live command smoke test (stop within 1 s); vision (frame →
local VLM → notes) once the CPU decision is in; Opus FEC only if concealment exceeds 1%.

## Acceptance targets (speech panel; test sessions frozen)
Name hit ≥95% quiet / ≥90% music; false wakes ≤1 per 10 min; short commands exact ≥90%;
WER ≤10% quiet, ≤18% music; speaker FRR@FAR1% ≤10% full turn, ≤25% at 1 s (else gate barge-in on
full turns only); false splits ≤5%, merges ≤2%, added wait p50 ≤600 ms; STT p95 ≤400 ms.

## Later
Discover listing: fix the onnxruntime pin clash, make the app work without the local Docker stack or
say so up front, then tag the Space (owner publishes). Release: kernel PR with CI green, simulator pinned
to the new kernel tag, publish from a squashed branch, owner approves each round.
