# Blueprint: an always-on agent kernel and its first body, rebuilt from what we learned

A from-scratch specification drawn from three sources: the previous kernel and
its Reachy pet app (2026-10-20), an earlier text chatbot (kani, text studies) and
an earlier Pipecat voice bot (measured voice). Every item is tagged **KEEP** (a standard we proved), **TEST** (a lesson
that becomes a failing test before any code), **BORROW** (proven elsewhere; cite
the source) or **DESIGN** (still missing; design it in, don't patch it on).

## 1. Shape

```
Bodies & tools (plugins)            Protocol            Kernel
─────────────────────────           ────────            ──────────────────────────────
Reachy · browser · phone ·   ◀──▶   hello+manifest  ◀──▶ identity · memory tiers ·
speaker · agent computer            events (+timing)     journal · authorize (risk tiers)
(sandbox: shell, web, MCP)          effects (streamed)   · decide (provider) · jobs/queue
Real-time loop stays here:          results per effect   · disposition (when to speak)
VAD, turns, barge-in, STT, TTS
```

- **KEEP** One loop: sense → event → decide (silence is a decision) → authorized
  effect → result. Fast loops never cross the protocol.
- **KEEP** Everything is a plugin, including the agent's computer; the kernel never
  gets a shell. Kernel core is dependency-free and owns all state; open-source
  frameworks live in adapters and never own identity or memory.
- **KEEP** Docker for every environment, a Makefile for every task, sealed
  containers, loopback-only ports, tokens per device, a black-box HTTP
  conformance suite any kernel or plugin must pass.
- **BORROW** Import boundaries enforced by a tool, not by convention
  (the earlier voice bot's import-linter; ours: `tests/test_boundary.py`).

## 2. Protocol (v1 target)

- **KEEP** hello + manifest, events, effects with a journal-sequence cursor;
  idempotent turn IDs; freshness by the kernel's clock; coded errors that say what
  to do next; path versioning.
- **DESIGN** Manifests shaped like MCP tool lists (`name`, `description`,
  `inputSchema`, plus `max_s`, `interruptible`); the same list every turn; an
  unavailable verb returns a clear error.
- **DESIGN** Every effect reports `done` / `failed` / `interrupted` with what was
  actually performed. **BORROW** an "interrupted" event so memory records only what
  was heard (the earlier voice bot's interruption handling; our `on_spoken` rule).
- **DESIGN** Streamed replies: `reply_delta` pieces delivered as the model writes;
  only the final reply is journaled.
- **BORROW** Per-turn timing reported by the device with each turn: `stt_ms`,
  time to first token, time to first audio (the earlier voice bot's metrics observer).
- **TEST** Stop reaches whichever body is acting, within one step (it did not).
- **TEST** Event rate limits per device; a flood is refused with a code.

## 3. Kernel

- **KEEP** Journal before acting; a decision and its outcome commit together;
  interrupted turns are never replayed and are cleared only by their own device.
- **BORROW** A compiled-prompt provenance record per decision: one frozen value
  that is both what the model receives and what is stored, so every reply traces
  to exactly what the model saw (the earlier chatbot's
  `CompiledPrompt`).
- **BORROW** Rolling summarisation instead of last-N exchanges, and "end the
  session if the model is unusable" rather than going mute (the earlier voice bot's
  unusable-model policy). **DESIGN** memory tiers on top:
  profile (always in the prompt), dated log, search; memories are information,
  never instructions.
- **BORROW** Rebuild context from the journal after a crash (the earlier voice bot's
  rejoin logic).
- **DESIGN** Native tool calling in the provider adapter (kani): manifest tools
  become function schemas; the kernel still authorizes every call. Engines are
  configuration, not code.
- **DESIGN** Disposition: quiet / available / engaged / restless, pure and
  clock-injected. **BORROW** idle follow-ups with a cooldown and once per idle
  period (the earlier chatbot) and pure presence logic with two
  grace periods (the earlier voice bot); proactive speech waits until the body
  is idle.
- **DESIGN** Background jobs and a per-conversation queue; long work never blocks
  the body; results arrive as events.
- **KEEP** Risk tiers enforced by the kernel (read / body / change / outside
  world, owner approval with "no answer means no"); untrusted sources tagged.

## 4. Body: the real-time loop (Pipecat inside the plugin)

- **BORROW** Barge-in on the VAD *onset* edge, about 160 ms earlier than the
  speaking edge, pushed upstream past the queue (the earlier voice bot's
  onset VAD).
- **BORROW** Interruption policy: a 600 ms floor for the bot, one interrupt per
  response, re-checked at each sentence start, stale onsets ignored after 3 s
  (the earlier voice bot's interruption tracker).
- **BORROW** Speak the first clause (4+ words) at once, then whole sentences
  (the earlier voice bot's clause aggregator).
- **BORROW** Turn end calibrated on real recordings, with Smart Turn v3 as a gate
  rather than a fixed silence (the earlier voice bot's
  pause-distribution analysis; its measured turn end: 450 ms VAD + 50 ms + 1.5 s backstop).
- **BORROW** Self-echo detection by word-set similarity with recent bot speech
  (the earlier voice bot, threshold 0.65). **TEST** the body never
  answers itself, with the robot's real playback lag.
- **BORROW** Swappable local services behind OpenAI-style URLs: Parakeet TDT
  ONNX for STT, Kokoro for TTS, vLLM/Ollama for the model (the earlier voice bot's
  local services).
- **DESIGN** A Reachy audio transport for Pipecat over the robot's WebRTC media
  and hardware echo cancellation.
- **KEEP** The LLM is not in the body's pipeline: a Sage processor sends the final
  transcript to the kernel and turns streamed reply text into TTS frames.
- **TEST** Emoji are never spoken; a body renders them as movement. Emotion
  labels and names are never spoken. Telling a model only "begin with X" yields
  only X.
- **DESIGN** Expressiveness neither prior project has: emotion as a tool call;
  the robot's own recorded sounds for wordless moments; short vocal sounds
  ("hmm", "aww") in speech; backchannels while the person talks.
- **DESIGN** Addressing: "was that meant for me?" before answering.

## 5. Measurement and tests

- **KEEP** Pure policy tests (ms) → conformance over HTTP (s) → simulator
  (tens of s) → hardware demo.
- **BORROW** SLO-gated load tests from a library of spoken conversations, with an
  LLM-judge rubric and WER/fragmentation scoring (the earlier voice bot's
  load tests; its SLO: reply p95 ≤ 2 s). **DESIGN** the same for
  the pet: target voice-to-voice p50 ≤ 1 s, p95 ≤ 2 s.
- **BORROW** A mock model with realistic (lognormal) latency for load tests, and
  integration tests that need keys kept off by default (the earlier chatbot's
  latency bench).
- **BORROW** An evidence ledger of measured before/after numbers per change
  (the earlier voice bot's ledger; ours: `bench/LEDGER.md`).
- **DESIGN** The Pollen MuJoCo simulator as a device: motion limits and
  look-at accuracy checked in the rendered camera frame.

## 6. Security lessons to carry over

- **TEST** No default credentials on any body; key-only SSH; control APIs reachable
  only from the kernel host.
- **TEST** An unset secret must refuse service, never skip auth; compare tokens in
  constant time (the earlier voice bot did neither).
- **TEST** No database or cache ports published to the host; no empty database
  passwords (both earlier projects' dev compose files did).
