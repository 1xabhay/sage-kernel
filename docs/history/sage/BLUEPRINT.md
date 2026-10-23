# Blueprint: an always-on agent kernel and its first body, rebuilt from what we learned

A from-scratch specification drawn from three sources: this kernel and the Reachy
pet (2026-10-20), chatbotlab (kani, text studies) and MeetLab (Pipecat, measured
voice). Every item is tagged **KEEP** (a standard we proved), **TEST** (a lesson
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
  (MeetLab `.importlinter`; ours: `tests/test_boundary.py`).

## 2. Protocol (v1 target)

- **KEEP** hello + manifest, events, effects with a journal-sequence cursor;
  idempotent turn IDs; freshness by the kernel's clock; coded errors that say what
  to do next; path versioning.
- **DESIGN** Manifests shaped like MCP tool lists (`name`, `description`,
  `inputSchema`, plus `max_s`, `interruptible`); the same list every turn; an
  unavailable verb returns a clear error.
- **DESIGN** Every effect reports `done` / `failed` / `interrupted` with what was
  actually performed. **BORROW** an "interrupted" event so memory records only what
  was heard (MeetLab interruption handling; our `on_spoken` rule).
- **DESIGN** Streamed replies: `reply_delta` pieces delivered as the model writes;
  only the final reply is journaled.
- **BORROW** Per-turn timing reported by the device with each turn: `stt_ms`,
  time to first token, time to first audio (MeetLab `bot.py` `_MetricsObserver`).
- **TEST** Stop reaches whichever body is acting, within one step (it did not).
- **TEST** Event rate limits per device; a flood is refused with a code.

## 3. Kernel

- **KEEP** Journal before acting; a decision and its outcome commit together;
  interrupted turns are never replayed and are cleared only by their own device.
- **BORROW** A compiled-prompt provenance record per decision: one frozen value
  that is both what the model receives and what is stored, so every reply traces
  to exactly what the model saw (chatbotlab `api/chatbot/services/prompt.py`
  `CompiledPrompt`).
- **BORROW** Rolling summarisation instead of last-N exchanges, and "end the
  session if the model is unusable" rather than going mute (MeetLab `bot.py`
  `_assistant_params`, `UNUSABLE_POLICY`). **DESIGN** memory tiers on top:
  profile (always in the prompt), dated log, search; memories are information,
  never instructions.
- **BORROW** Rebuild context from the journal after a crash (MeetLab
  `packages/core` `rejoin.py`).
- **DESIGN** Native tool calling in the provider adapter (kani): manifest tools
  become function schemas; the kernel still authorizes every call. Engines are
  configuration, not code.
- **DESIGN** Disposition: quiet / available / engaged / restless, pure and
  clock-injected. **BORROW** idle follow-ups with a cooldown and once per idle
  period (chatbotlab `services/followup.py`) and pure presence logic with two
  grace periods (MeetLab `presence.py`); proactive speech waits until the body
  is idle.
- **DESIGN** Background jobs and a per-conversation queue; long work never blocks
  the body; results arrive as events.
- **KEEP** Risk tiers enforced by the kernel (read / body / change / outside
  world, owner approval with "no answer means no"); untrusted sources tagged.

## 4. Body: the real-time loop (Pipecat inside the plugin)

- **BORROW** Barge-in on the VAD *onset* edge, about 160 ms earlier than the
  speaking edge, pushed upstream past the queue (MeetLab
  `speech_onset_vad.py`, `bot.py` `make_speech_onset_handler`).
- **BORROW** Interruption policy: a 600 ms floor for the bot, one interrupt per
  response, re-checked at each sentence start, stale onsets ignored after 3 s
  (MeetLab `interruption.py` `InterruptionTracker`).
- **BORROW** Speak the first clause (4+ words) at once, then whole sentences
  (MeetLab `clause_aggregator.py`).
- **BORROW** Turn end calibrated on real recordings, with Smart Turn v3 as a gate
  rather than a fixed silence (MeetLab `scripts/analyze-pause-distribution.py`,
  `smart_turn.py`; their measured turn end: 450 ms VAD + 50 ms + 1.5 s backstop).
- **BORROW** Self-echo detection by word-set similarity with recent bot speech
  (MeetLab `bot.py` `_text_similarity`, threshold 0.65). **TEST** the body never
  answers itself, with the robot's real playback lag.
- **BORROW** Swappable local services behind OpenAI-style URLs: Parakeet TDT
  ONNX for STT, Kokoro for TTS, vLLM/Ollama for the model (MeetLab
  `services/stt/app.py`, `docs/latency-options.md`).
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
  LLM-judge rubric and WER/fragmentation scoring (MeetLab `tools/loadtest`
  `judge.py`, `quality.py`; their SLO: reply p95 ≤ 2 s). **DESIGN** the same for
  the pet: target voice-to-voice p50 ≤ 1 s, p95 ≤ 2 s.
- **BORROW** A mock model with realistic (lognormal) latency for load tests, and
  integration tests that need keys kept off by default (chatbotlab
  `api/bench_latency.py`, `pytest.ini`).
- **BORROW** An evidence ledger of measured before/after numbers per change
  (MeetLab `infra/LEDGER.md`).
- **DESIGN** The Pollen MuJoCo simulator as a device: motion limits and
  look-at accuracy checked in the rendered camera frame.

## 6. Security lessons to carry over

- **TEST** No default credentials on any body; key-only SSH; control APIs reachable
  only from the kernel host.
- **TEST** An unset secret must refuse service, never skip auth; compare tokens in
  constant time (MeetLab `runner.py` `verify_api_key` does neither).
- **TEST** No database or cache ports published to the host; no empty database
  passwords (chatbotlab and MeetLab dev compose files).
- Owner check: `chatbotlab/api/.env.example` matches an API-key pattern; Terraform
  plan/state files sit in MeetLab `infra/`. Review, and rotate anything real.
