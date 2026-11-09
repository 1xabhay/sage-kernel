# 00: Sage target layer design, eval gates, migration plan

Inputs: 01 (device as-is), 02 (kernel), 03 (external), 04 (evals), 05 (ops), PLAN.md, owner memory. Device paths are `D:` = `devices/reachy-mini/apps/reachy_sage_voice/reachy_sage_voice/` at voice-prep fef8522. Kernel paths are `K:` = `sage/`. Every file:line that drives a decision below was spot-checked in code and held.

**Shape in one line.** This is Gat's three layers (03 §1), with Pollen's MovementManager as the controller (03 §2). The device holds a reflex and a sequencer, and the motion arbiter is the only writer to the robot. The kernel deliberates and remembers, and it is never in the motion loop. All of it runs on the Mac, and nothing new goes on the Pi (05 §1).

---

## 0. Resolved tensions

| Tension | Decision | Why |
|---|---|---|
| **Latency 1.6–1.9 s (02) vs 2.4 s (04)** | 02's figure is a **stage-sum estimate** from bench parts (02 §5). 04's is **measured** over 421 live turns of the device's `first_audio_ms`, timed from the last voiced frame (VAD stop − `stop_secs`, `D:sage.py:208`) to the first audio pushed to the transport (`D:sage.py:431`). That clock *excludes* the 150 ms playback lead and the WebRTC buffer, so the live truth is ≥ 2.4 s. The gap is serial STT backlog, a hello on every turn (`D:kernel.py:211`), cold prefill and guard retries. **Canonical metric: `v2v_ms` = last voiced frame → first audio frame handed to the robot transport + a constant lead measured once on the robot.** Reported as p50/p95 per lane (`reflex`, `kernel`), always from logs, never as a sum. Interim gate: kernel p50 ≤ 1.5 s, p95 ≤ 3.0 s. Milestone target, unchanged (LEDGER): p50 ≤ 1.0 s, p95 ≤ 2.0 s. | Only a measured end-to-end number can gate. p95, not p90, matches the LEDGER |
| **Joint limits** (SDK ±160 / ≤65°; code 179; measured −157) | **Source of truth: one `motion/limits.py` table, written from `robot/calibration/reach_probe.py` output, each value dated.** Effective limit = min(SDK clamp, measured) − 2° margin. So: body ±158; head relative to body ±53 (measured ±55 sits inside the SDK's 65); absolute look reach **+171 left / −155 right**; pitch and roll ±40; body gain knob for the 3–5° undershoot. `REACH = 179` (`D:executor.py:216`) is retired. | The daemon silently drops out-of-limit targets, which reads as a freeze (04 A5). Hardware drifts, so the knob stays and the probe is re-run after the firmware 2.1.4 / daemon 1.12.0 update |
| **Plan ownership** | **The device owns the skill schema.** One `skills.py` declares each skill (name, arg kind, enum/range, `until`, budget). From it, *both* `parse()` and the manifest's `inputSchema` are generated. The kernel stays generic: it validates against the declared JSON Schema (`K:manifest.py:111-148`) and never learns skill semantics. This matches 02 §4 and keeps the kernel "never learns what a device is" (`K:adapters/http.py:3`). | Fixes two schemas for one plan (01 §4.9: numeric `turn` refused by the kernel) |
| **LLM in the command path** | **Grammar-only is the default from L2.** A constrained fallback (the kernel's decide-then-plan, 02 v2 #3) is adopted **only if eval gate G-LLM passes** in L8 (gate in §4). If it fails, `body_plan` is removed from the manifest, which also frees about 600 prompt tokens for latency. | 5 models scored 0–1/11 on plan recall (02 §3). The owner wants numbers, not hope |
| **Other bodies?** (unanswered) | Default: **"not now, keep the door open"**. Layers talk only through the contracts in §2, so the answer changes one folder move and four kernel v2 items. Nothing is built for it now. See §2.5. | YAGNI with a cheap exit |

---

## 1. Target layers

### 1a. Responsibility and contracts

| # | Layer | Responsibility | In → Out |
|---|---|---|---|
| 1 | **Perception** | Mic transport + ring buffer, VAD onset/stop, Smart Turn, DoA, face reads (one source, freshness-checked), beat/music, owner voiceprint | robot media, daemon HTTP → `Percept` events: `speech.start/stop`, `doa`, `face`, `music`, each with `t` and `fresh` |
| 2 | **Speech I/O** | STT, echo filter, name correction, TTS, clause chunking, paced output, duck-then-confirm barge-in (03 §2) | `speech.stop` + audio → `Heard{text, conf?, t_end}`; `Say{text, style}` → audio, `bot.speaking start/stop` |
| 3 | **Intent router** | A pure function: `route(Heard, Context) → Command(plan) \| Song \| KernelTurn \| Drop`. It holds the grammar reflex, and Stop always wins. The constrained fallback lives in the kernel (gated) | `Heard` → one routed intent + `Ack` line |
| 4 | **Behaviour sequencer** | Runs plans as nodes (Running/Success/Failure) under `asyncio.timeout`. Owns the one activity slot, search, track handoff, dance and song listening. Speaks outcomes; emits `acted` | `Plan` → `MotionIntent`s to the arbiter; `PlanResult` → kernel link + `Say` |
| 5 | **Motion arbiter** | **The only writer to the robot.** A 50 Hz loop: one primary source (goto, emote clip `evaluate(t)`, dance, search view, breathing) + additive offsets (aliveness drift, listening freeze, speech wobble). Clamps to `limits.py`. Owns tracking weight and motors, and re-asserts its desired state on every link-up | `MotionIntent`, `Offset` → `set_target` / tracking weight; `motion.link` events |
| 6 | **Aliveness** | Breathing base layer (5 mm at 0.1 Hz, antennas 15° at 0.5 Hz, 03 §2), 2–3 incommensurate drifts, glances with habituation, listening/thinking/speaking postures | percepts, `bot.speaking`, `plan.*` → `Offset` / low-priority `MotionIntent` |
| 7 | **Kernel link** | Protocol client: one `Session` per body and one hello per connect (not per turn). Effect dispatcher keyed by kind; `acted`/`result`/`notes`/`presence` out | `KernelTurn`, `PlanResult`, `Aside` → HTTP; effects → `Say`, `Plan`, `Emote` |
| 8 | **Supervision** | Robot reachability with retry (no exit), media reset, beam check/re-route, service health, heartbeat file for `status.py` | `health` events from every layer → recovery actions, `status` lines |
| 9 | **Services** | STT :8781, TTS :8782, research :8783 + SearXNG, kernel :8776, Ollama. Unchanged contracts (01 §5); the TTS engine is swappable (owner chose Kyutai Pocket TTS, azelma 0.6) | HTTP |

### 1b. Placement, budgets, state, failure

| # | Runs | Timing budget | Owns state | Failure behaviour |
|---|---|---|---|---|
| 1 | Mac, bot process (daemon detector on the Pi, as today) | onset → event ≤ 50 ms; face older than 1.5 s or detector off ⇒ `fresh=false` | sensor freshness, last-seen face bearing | Camera dead ⇒ `face.fresh=false`, never "no face". Mic silent 3 s ⇒ `health` to supervision |
| 2 | Mac bot + Colima services | STT ≤ 150 ms p50 per segment; TTS first clause ≤ 250 ms; duck ≤ 100 ms after onset | recent TTS text (echo), playback | STT down ⇒ emote "can't hear" ≤ 3 s; TTS down ⇒ emote instead of speech (R3) |
| 3 | Mac, in process | ≤ 5 ms | none (pure; context passed in) | Unknown ⇒ `KernelTurn`. Never raises |
| 4 | Mac, in process | command → first `MotionIntent` ≤ 100 ms; every node has a budget; search ≤ 60 s total | activity slot, plan progress | Budget hit ⇒ `failed:timeout`, spoken line + emote, home pose. Every plan ends with a result (A3) |
| 5 | Mac, **one dedicated thread** | 50 Hz tick, p95 jitter ≤ 10 ms; link stall ⇒ hold, never queue | desired pose, tracking weight, motor state | Link down ⇒ stop sending, keep desired state, re-assert on `motion.link up`. Out-of-limit target ⇒ clamp + count (must stay 0) |
| 6 | Mac, in process (feeds 5) | thinking cue ≤ 500 ms after `speech.stop`; still ≤ 5 s while awake | habituation scores | Lowest priority; any primary preempts it. Can only pause, never stop forever |
| 7 | Mac, in process | hello once per connect; no added round trip per turn | cursor, turn ids | Kernel down ⇒ "one moment" after 5 s busy-retry (owner rule), reflex lane unaffected |
| 8 | Mac, in process; `sage-health` on the Pi (exists) | detect ≤ 2 s (link), ≤ 3 s (mic); R1 ≤ 60 s, R2 ≤ 30 s | health table, heartbeat file | Retry with backoff forever. Exit only when media reset fails (launchd restarts once Local Network is granted) |
| 9 | Colima (sealed), Ollama native | kernel first reply piece p50 ≤ 0.8 s | journal (kernel), never touched by tests | compose `unless-stopped`; never `down -v` |

**Change from the expected shape:** the constrained fallback sits in the **kernel**, not in the router. Ollama is single-slot and the kernel is its only client (05 §3, one GPU job at a time). A device-side model call would contend with the kernel and would not be journaled. The router stays pure.

---

## 2. Contracts

### 2.1 Event bus (in process, no broker)

```python
@dataclass(frozen=True)
class Event:            # every percept, intent and outcome
    kind: str           # "speech.start" "speech.stop" "heard" "doa" "face" "music"
                        # "bot.speaking" "plan.start" "plan.end" "motion.link" "health"
    t: float            # time.monotonic() at the source; t_end for speech.stop = last voiced frame
    source: str         # layer name
    turn_id: str | None
    data: dict          # typed per kind, e.g. face: {detected, x, y, detector_on, fresh}
```
The bus is a list of `asyncio.Queue`s per subscriber (stdlib). Arbiter-bound intents cross to its thread through one `queue.SimpleQueue`. Every log line carries `t` and `turn_id`, which makes E2, A2 and R1 scoreable (04 §7 housekeeping).

### 2.2 Skill and plan schema (device-owned)

```python
SKILLS = {  # skills.py: the single source; parse() and inputSchema both derive from it
  "look":        Skill(arg=Enum(LOOKS)),
  "turn":        Skill(arg=Degrees(-158, 158) | Literal("around")),   # schema: oneOf number|"around"
  "search_face": Skill(until=("face_found",), budget_s="from_pace"),
  "track_face":  Skill(until=("stop_tracking",)),
  "stop_tracking": Skill(), "dance": Skill(timeout_max=MAX_DANCE_S),
  "listen_song": Skill(), "emote": Skill(arg=Enum(EMOTIONS)), "stop": Skill(),
}
Plan = list[Step]  # ≤ 4 steps, open-ended last
PlanResult = {"plan_id", "origin": "reflex"|"kernel", "status": "done"|"failed"|"interrupted",
              "reason"?, "said"?, "t_start", "t_end"}
```
Contract test: every schema-valid instance parses, and every parsed plan re-serializes to a schema-valid one (property test). `EMOTIONS`, `manifest()`, `voice.tools` and `songs.TOOL` move here out of `D:kernel.py:20-22,95-124`, and `reachy_sim` imports them instead of copying (01 §8.9).

### 2.3 `acted` event (device → kernel, v1.1)

```json
{"kind": "acted", "act_id": "a-17", "origin": "reflex",
 "what": "body_plan", "input": {"steps": [{"skill": "search_face"}]},
 "status": "failed", "reason": "not_found", "said": "I looked everywhere, Mika."}
```
`what` is `body_plan` or `aside` (a line spoken outside a turn, `D:sage.py:536-539`). The kernel journals it and renders it as a tool message in the next exchange, as it does done-text today (02 §10 #1). It is rate-limited like other events and needs no model call. Kernel-originated plans keep using `result`.

### 2.4 Health

```json
{"layer": "perception.face", "ok": false, "since": 1234.5, "detail": "detector off"}
```
Every layer publishes one per 2 s on the bus. Supervision writes the table to the existing heartbeat file, and `status.py` prints one line per layer. Kernel: `GET /v1/health` → `{"ok", "protocol": 1, "features": [...]}`. Today there is no health route and `status.py` accepts any answer (05 §6).

### 2.5 Protocol changes

| Change | Version | Needed for |
|---|---|---|
| `acted` kind (2.3) | **v1.1 additive** | bug 6 |
| `submitted.input {medium, confidence?, alternatives?}`; the kernel uses it instead of the ≤ 4-word rule (`K:policy.py:256`) | v1.1 | command priming, names |
| `hello` response `features`; `GET /v1/health` | v1.1 | health, graceful degrade |
| tool flag `"command": true` → kernel decide-then-plan | v1.1 (only if G-LLM passes) | fallback |
| per-device turn lanes, per-device history rendering, `outputs/medium`, namespaced timing (02 v2 #4–6) | **v2, only if a second body is scheduled** | multi-body |

**Shared versus Reachy-only, under both answers:**

| Part | No other bodies | Other bodies (phone/speaker/robot) |
|---|---|---|
| kernel, protocol, journal, services | shared | shared + v2 items above |
| bus, `Event`, health, kernel link | in the Reachy app | moved to `devices/common/sage_device/` |
| speech I/O, router mechanics | in the Reachy app | moved to common; grammar *vocabulary* stays per body via `skills.py` |
| skills.py, sequencer, arbiter, aliveness, perception adapters, supervision (beam, media, daemon) | Reachy-only | Reachy-only |

Rule for now: no layer imports another's internals, only its contract module (enforced by an AST import test in `make imports`). Then the "yes" answer is a `git mv` of four folders and nothing else on the device.

---

## 3. How each live bug disappears

| Bug | Root (01 §7) | Under the design |
|---|---|---|
| **Commands not obeyed** | regex fullmatch only (`D:grammar.py:110-116`); optional `body_plan` field; numeric `turn` refused by schema (`D:plan.py:124`); failures silent; transcripts dropped during song (`D:sage.py:213`) | Router grammar widened against the golden set (I1). The schema is generated from `skills.py`, so numeric turns pass. The sequencer speaks every failure. Stop and commands route even while a song is being listened to. The LLM path is either proven by G-LLM or removed |
| **Search stuck** | `hold()` on every VAD start including Sage's own voice (`D:sage.py:200-205`); hold extends the deadline (`D:executor.py:428-430`); lost VAD stop leaves the hold on; symmetric 179 reach; budget from planned pace | No hold flag. The arbiter pauses *primary* motion only while an `owner speech` percept is fresh, ignores speech while `bot.speaking`, and auto-expires the pause after 8 s. The search node uses the owner's 3-sweep path clipped to `limits.py`, with a wall-clock budget derived from measured pace (4.1 s/view), dwell before "not found" (03 §4), and a spoken failure |
| **Robot static** | idle is a vetoed fallback (8 vetoes, `D:executor.py:536-549`); tracking vetoes idle; idle queued behind 0.6 s gotos | Breathing is an always-on additive layer under everything, tracking included (antennas + z). No more exclusive modes. One 50 Hz writer, no goto queue. Gate: A6 still time ≈ 0 |
| **Face always false** | detector runs only at weight > 0 (daemon `abstract.py:678-713`); tracking off by default (`D:__main__.py:161`); no `ts` check; weight cache stale after reconnect (`D:executor.py:906-909`, `D:robot.py:95-96`); camera ribbon | `face` percepts carry `detector_on` and `fresh`; presence is **unknown, never false**, when the detector is off, so the kernel gets no false "lost". The arbiter owns weight, drops the cache on link-up and re-asserts *its* desired weight (not 1.0). A detector refusal surfaces as `health`. Hardware: reseat the ribbon, then P4 |
| **Name misheard** | "Mika" never transcribed; heard-list fixes 15/26 (04 P2); the misspelt name was remembered | Speech I/O corrects from the logged-variant table *before* routing and before the kernel; `submitted.input.alternatives` lets the kernel see doubt. The owner re-teaches through conversation (never hand-edit). Gate P2 |
| **Kernel blind to the body** | reflex acts and asides never reach it (02 §4) | `acted` on every reflex plan end and every aside, journaled and shown in the next exchange (conformance test) |

---

## 4. Eval gates

Tiers: **CI** = blocking on every PR (U + sim, CPU). **Replay** = blocking before `make deploy` (recorded audio or logs, CPU, plus GPU-lane runs). **Live** = owner + robot checklist, serial lane, before calling a phase done.

| Layer | Evals (04) + new | Threshold | Tier |
|---|---|---|---|
| Perception | P1 WER | owner quiet ≤ 12%, music 1.5 m ≤ 25%, commands ≤ 5% | Replay (after the owner labels recordings2) |
| | P3 false triggers | own voice ≤ 1%; TV ≤ 1 turn/10 min; 0 body commands from songs | CI (#7 fixture), Replay, Live ambient |
| | P4 face recall | ≥ 90% to 2 m, ≥ 70% at 3 m, ≤ 0.1 false/min | Live (hardware gate) |
| | **new F1** face freshness: stale or detector-off ⇒ `fresh=false`, presence never "lost" | 100% | CI |
| Speech I/O | P2 names | variants → Mika 100% (table), ≥ 95% on audio; 0 "Micah" in replies | CI (#4), Replay |
| | E1 `v2v_ms` | kernel p50 ≤ 1.5 s, p95 ≤ 3.0 s (target 1.0/2.0) | Replay on live logs (#9) |
| Router | I1 command accuracy | combined ≥ 90% owner+misheard, ≥ 97% clean, stop 100% | CI (#1) |
| | I2 confusion | false action ≤ 1/50, song recall ≥ 90% | CI (#10) |
| Sequencer | A1 search coverage | planned ≥ 98%, achieved ≥ 95% at live pace, 0 counter-rotations | CI (#3), sim, Live |
| | A2 time to find | p90 ≤ 25 s; no-face ends ≤ 60 s with complaint | sim, Live |
| | A3 no-stuck | 0 stuck; every plan resolves ≤ budget + 5 s | CI property test; 2 h sim soak pre-deploy |
| | E2 first motion | reflex command p95 ≤ 0.8 s; thinking cue ≤ 0.5 s | CI (#8 timestamps), Replay |
| Arbiter | A5 limits | 0 targets beyond `limits.py`, 0 counter-rotations | CI hard gate (#5) |
| | **new M1** single writer: limit-asserting `fake_mini` fails any SDK call off the arbiter thread | 0 | CI |
| | **new M2** tick rate | ≥ 48 Hz achieved, p95 jitter ≤ 10 ms (sim); live ≥ 40 Hz under daemon load | sim CI, Live |
| Aliveness | A4 idle variety | ≥ 30% range, entropy ≥ 0.9·log2 3, CV ≥ 0.25; owner ≥ 4/5 | CI (seeded), Live |
| | **new A6** still time while awake and idle | ≤ 5 s continuous, ≤ 2% of awake time | sim CI, Live |
| Kernel | D1 answer quality, self-knowledge | ≥ 90% rubric, 4/4 | Replay (GPU lane) |
| | D2 memory | 100% mechanics, ≥ 90% model, 0 wrong names | CI (conformance), Replay |
| | D3 plan validity | ≥ 99% valid, 0 unparsed reach the executor | CI (#2 + contract test) |
| | **new K1** `acted` journaled and rendered; `input.medium=text` never "(may be misheard)" | 100% | CI (conformance) |
| Supervision | R1 reboot | ≤ 60 s, 0 manual steps | CI (#6), Live (3 trials) |
| | R2 media/beam | ≤ 30 s, beam restored | CI fake, Live |
| | R3 service down | 5/5, no traceback exit | CI integration (#11) |
| **G-LLM** | I1 + I2 with fallback on vs off, real STT transcripts, real Ollama | adopt iff combined I1 rises ≥ 5 pts **and** false action ≤ 1/50 **and** fallback-path p95 adds ≤ 1.0 s **and** D1 does not drop | Replay (GPU lane), once |

Precondition for CI as a gate: fix `test_eval.py::test_kernel_relevant_imports_read_only_from_a_path` (04).

---

## 5. TDD migration plan

Strangler: each phase is a red commit, then a green one, then a `make deploy SHA=` and a short live check. **Rollback for every phase is `make deploy SHA=<previous>`** (05 §6), and a kernel change rolls back with `SAGE_TAG=<sha> make up-tag`. No phase touches the journal, and every kernel test runs on throwaway state.

**Where it lands.** Layer folders go inside the app package: `D:{perception,speech,intent,behaviour,motion,alive,kernel_link,supervise}/` plus `D:bus.py` and `D:skills.py`. That means no new top-level dirs and no new repo (owner rule). Kernel changes go to `sage/` before the Phase E move and to `kernel/sage/` after it.

**Fit with PLAN.md.** Nothing starts before **B3 (sage-voice) and B1 (notes) merge**, since voice-prep is that line of work. L0–L1 run alongside Phase C. **L4 (kernel v1.1) must merge before Phase E's kernel → kernel/ move**, because that move is gated on an empty backlog. L8's kernel work comes after the move. The bench → `devices/reachy-mini/bench` move (Phase E) does not touch the layer folders.

| Phase | Lane | Red tests first | Change | Turns green | Rollback note |
|---|---|---|---|---|---|
| **L0 Baseline** | CPU P + 1 live S | #8 E2 timestamps; scorer for `v2v_ms`; fix the failing eval test | timestamps + `turn_id` on every log line; `plan.start` log; measure transport lead once | E2 measurable; record E1/A1/A4 baselines | log-only |
| **L1 Contracts + move-only split** | CPU P (one owner: it touches the hot files) | #2 D3; schema↔parse property test; #5 A5 with limit fake; M1 (expected red) | `skills.py`, `limits.py`, `bus.py`; move code from `sage.py`/`executor.py` into layer folders **with no behaviour change** except the generated schema and clamps; sim imports `skills.py` | D3, A5, contract test | behaviour identical; robot works |
| **L2 Router + names** | CPU P (owns `intent/`, `speech/`) | #1 I1, #4 P2, #7 P3, #10 I2 | pure `route()`; grammar widened from `commands_real.jsonl`; variant table; echo drop; commands pass during song | I1, I2, P2, P3-CI | Live: owner command block C |
| **L3 Supervision** | CPU P (owns `supervise/`) | #6 R1, #11 R3, R2 fake | retry, no exit at start; per-layer health; status lines; optional auto beam-on (decision O5) | R1, R2, R3 CI | Live: 3 daemon restarts |
| **L4 Kernel v1.1** | CPU P (kernel worktree) | K1 conformance: `acted`, `input.medium`, `features`, `/v1/health` | additive kernel kinds; device sends `acted` + `input`; drop hello-per-turn (`D:kernel.py:211`) | K1, conformance 50+ green | kernel `up-tag` |
| **L5 Motion arbiter** | dev CPU P, acceptance **S** | M1, M2, A6, A3 property | one 50 Hz writer thread; emotes via `evaluate(t)`, dance and idle as sources; breathing additive; tracking weight owned + re-asserted; behind `SAGE_VOICE_ARBITER=1` for **one** deploy, then the flag and the old path are deleted | M1, M2 sim, A3, A5, A6 | flag off = old path; Live: idle 5 min, owner rating, link stall |
| **L6 Sequencer + search** | CPU P → **S** | #3 A1 at 4.1 s/view, A2 sim, no-hold test (own voice does not pause motion) | nodes with budgets; search on `limits.py` with a pace-derived budget and dwell; bounded owner-speech pause; spoken outcomes → `acted` | A1, A2 sim, A3 | Live: 8 bearings × 2 distances |
| **L7 Perception + aliveness** | CPU P → **S** (needs reseated camera) | F1 freshness; presence unknown when the detector is off; E2 thinking cue | one face source with `ts`; DoA/face out of the executor; listening/thinking/speaking postures; habituating glances | F1, E2, A4 | Live: P4 grid, A4 rating |
| **L8 G-LLM decision** | **S** (GPU, one job) | G-LLM harness on I1/I2 sets via real STT text | run both arms; then either kernel decide-then-plan behind `"command": true` **or** remove `body_plan` from the manifest | G-LLM verdict; E1 after | manifest revert |

Ordering: L0 → L1 (blocks everything; nobody else edits `sage.py`/`executor.py` meanwhile) → {L2, L3, L4} in parallel worktrees by folder ownership → L5 → L6 → L7 (L7's CPU part may start after L5) → L8 after Phase E. The serial lane takes, in order: L0 live baseline, L2 check, L3 restarts, L5 acceptance, L6 search, firmware update + re-run probes, L7 camera, L8 GPU.

---

## 6. Owner decisions

**2026-10-25, owner:** O1 = (c): more bodies with different capabilities are coming (revised the same day from (b)), so the shared layers go to `devices/common/sage_device/` in L1b and kernel v2 items #4–6 + #8 are in scope; O4 = (b) voice-first + no-revisit (L6 search node changes accordingly: DoA/last-seen prior, inhibition of return, ~15 s give-up with a spoken call, then the A1/A2 gates still apply); O5 = yes; everything else approved as recommended.

| # | Question | Options | Rec |
|---|---|---|---|
| O1 | Other bodies (phone, speaker, second robot)? | (a) no; (b) maybe later; (c) yes, scheduled | **DECIDED (c)**: common package in L1b; kernel v2 (per-device turns, per-device history, output media, token reload) |
| O2 | LLM in the command path | (a) grammar only; (b) kernel decide-then-plan fallback; (c) today's optional field | **Decide by G-LLM**; (a) until then, never (c) |
| O3 | Presence when not tracking (the detector only runs at weight > 0) | (a) report unknown; (b) periodic low-weight detector bursts; (c) Mac-side detector on video | **(a)** now; try (b) only after the ribbon reseat and P4 pass |
| O4 | Search path | (a) owner's 3-sweep path, fixed to measured limits and pace; (b) DoA-first prior + inhibition of return (03 §4) | **DECIDED 2026-10-25: (b)**: start at voice direction + last-seen face, skip checked bearings, dwell, give up at ~15 s and call out |
| O5 | Re-run beam-on after a robot reboot automatically | yes / no | **Yes**, from supervision via `chip.py` |
| O6 | Owner-speech motion pause cap (upstream #1334) | none / 8 s / other | **8 s**, ignored while Sage speaks |
| O7 | Latency gate | interim 1.5/3.0 s → target 1.0/2.0 s | **Interim now**, target at milestone |
| O8 | Grant Local Network to uv's python3.12 (launchd) | grant / keep hand-run | **Grant**: nothing restarts on its own until then |
| O9 | Firmware 2.1.4 + daemon 1.12.0 | schedule in the serial lane | **Before L5** acceptance; re-run reach probe |
| O10 | Camera ribbon | reseat / leave | **Reseat** before L7 |
| O11 | DHCP reservations, persistent guard, host-key alias | do / defer | **Do** (05 §10.5) |
| O12 | SSH password auth | disable / keep | **Disable** after key-only is confirmed |
| O13 | Speaker volume in the voice app's status | add / skip | **Add** (persistence unverified) |
| O14 | PR merge order | #14, #8, #11 + fix, #7, #6, #9, #10, v2 (includes #5), layers squash | **Revised 2026-11-05: guards first resolves shared files once**, then `make hooks` |

---

## 7. Risks and scope

| Risk | Mitigation |
|---|---|
| 50 Hz `set_target` over WS to a CPU-starved daemon stalls (05 §1) | M2 live gate; the arbiter drops ticks, never queues; fall back to 25 Hz (`ponytail:` knob) |
| L1's move-only split conflicts with in-flight branches | starts only after B1/B3 merge; one owner; no behaviour change in the same PR |
| The camera may never pass P4 | presence stays unknown; search fails cleanly with a spoken line; nothing depends on vision |
| Sim says nothing about audio, AEC or feel (03 §5) | audio gates are Replay/Live only |
| GPU contention skews D1, E1 and G-LLM | serial lane, one job at a time (05 §3) |
| `acted` floods the history window (8 exchanges) | one `acted` per plan end, asides only when spoken; render inside the exchange, not as a new one |

**Out of scope:** anything new on the Pi; learned VLA/visuomotor policies; full-duplex models; a wake word; the Rust sage-os kernel; v2 items #4–6 and #9 (per-device lanes, history per device, outputs, fact provenance) unless O1 = (c); custom recorded moves; a behaviour-tree library (stdlib `asyncio.timeout` nodes are enough); a message broker; changes to the journal format.
