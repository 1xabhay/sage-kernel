# Changelog

All notable changes to the Sage kernel and its device protocol are recorded
here. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/);
the package follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: anything may change). The device protocol is versioned on its own,
as an integer (see [docs/PROTOCOL.md](docs/PROTOCOL.md#status-and-versioning)),
and every protocol change is listed under **Protocol**.

## [Unreleased]

### Added

- **Protocol** (v1.1, additive): `moves` in a manifest and the `moves` feature. A
  turn may end with a move and no words on any body; a direct question or a line of
  distress is never silent (retried once, then a short line of the kernel's own).

- `SAGE_DEVICES_FILE`: device tokens in a file, re-read on change without a
  restart; a removed token is refused, an added one accepted, a bad file keeps
  the old tokens and is logged. `SAGE_DEVICES` alone works as before.
- Notes: a device can hand the kernel a topic and up to 64 passages (such as a
  song looked up on the web); on each turn they match, the first and the 3 that
  best match the newest line ride on the newest message, as information. Journaled and checkpointed.
- Prompt cache warm-up: when a device says hello with a manifest the kernel has
  not seen from it (the first hello after a restart, or a changed body), the
  kernel sends Ollama one background call with that device's next prompt and
  `num_predict: 1`, so its next turn skips the ~6 s cold prefill. Never while a
  turn runs, abandoned when one starts, journals nothing (stderr says `warmed`).
- Model timing: the `decided` row keeps Ollama's own counts and durations from
  its final chunk as `model_timing` {prompt_tokens, prefill_ms, gen_tokens,
  decode_ms, load_ms}; `bench/latency.py` reports prefill and decode p50/p95.
- `sage.policy.relevant(passages, query, k)`: the BM25 retriever that picks
  which notes ride on a turn, importable on its own so a device's eval can score
  the kernel's own retrieval.
- `bench/audio_llm.py`: Gemma 4 E4B/E2B audio-in (llama.cpp) measured against
  Parakeet STT on 58 synthetic clips and 20 live ones; verdict no (worse WER
  outside clean speech, and its joint "heard + reply" JSON invents what it
  heard). Results in `bench/LEDGER.md`.

### Protocol

- v1.2, per-device turn lanes (§1, §3, §5, §7): each device has its own turn.
  Another body's turn no longer makes a device `busy`; a turn cut off by a
  restart is `unknown_pending` only for its own device. A bare `stop` still
  stops every body; a `stop` naming a turn reaches only the sender's own turn
  and effects (a device naming another device's turn now gets `409 stale`).
  A body's `acted` reports wait for its own next exchange (at most 4 per
  body); before, any device's next exchange took them all. A greeting or
  nudge waits only on its own device's turn and effects; before, any body's
  running or `unknown` turn held every greeting.
  Still protocol `1`; a lone device sees no change. The checkpoint gains
  `lanes` (the other devices' turns); earlier checkpoints read as one lane, and
  v1.1 kernels still read the new ones.
- New error `409 hello_first` (§2, §7): `POST /v1/events` and `GET /v1/effects`
  refuse a device that has not said hello since the kernel started, so no turn
  runs without its manifest. The device says hello again and retries, as for
  `reset`. A device that says hello on every connect only sees it after a
  restart it did not notice; before, its turns ran with no tools and no body.
- v1.1, additive (§1, §3): new event `acted` `{act_id, origin, what, input?,
  status, reason?, said?}`, a body's own plan or aside, journaled and told to the
  model inside its next exchange; `submitted.input` `{medium, confidence?,
  alternatives?}`, so a typed or confidently heard line is never "(may be
  misheard)" (the 4-word guess stays for devices that send none); `features` in
  the `hello` answer; `GET /v1/health` `{ok, protocol, features}`, with a token.
- Manifests may carry `body`, the body's own description in at most 300
  characters (§2). The model sees it beside the senses and may name parts it
  cannot use yet ("I have a camera, but I can't see through it yet"); it declares
  no sense. Manifests without it work as before.
- A `done` result's `text` for a tool effect now reaches the model on the next
  turn, after that exchange, as the body's report (§5); before, only failures did.
- New event `notes` `{topic, passages}` (§10, draft). JSON bodies may be up to
  131072 bytes (was 8192). Notes ride only on turns they match (a passage after
  the first matches the line, or the line names the topic); an unrelated line
  gets none.

## [0.2.0] - 2026-10-21

### Added

- Remembered facts, as Pollen's Reachy Mini conversation app does it: the model
  may add `remember` or `forget` to its answer; facts are journaled, kept in the
  checkpoint (up to 20, 120 characters each) and rendered at the end of the fixed
  prompt prefix, so a name outlives the history window and a restart.

### Removed

- Guards that an eval ablation on qwen3:4b-instruct showed do nothing measurable:
  the act-don't-announce retry, the assistant-speak filter and its retry, the
  hotter (0.9) retry temperature, the `SAGE_REPEAT_FALLBACK` experiment, hiding
  repeated replies from the model, and dropping or holding recently said
  sentences. The repeat retry, the sense guard, misheard framing, the reminder
  line and "never unsay" stay.

### Changed

- The prompt cache now holds across turns once the window is full: the history
  is cut from 8 back to 5 exchanges in one block instead of sliding by one
  every turn, which made Ollama re-read the whole history on every turn.

## [0.1.0] - 2026-10-21

The first public release, with device protocol v1 in Draft.

### Protocol

- Device protocol v1 (Draft): MCP-shaped tool manifests, a result per effect,
  Stop delivered to every acting device, streamed reply pieces
  (`reply_delta`), timing and `duration_ms` reported by the device, a
  per-device event rate limit, coded errors, presence and kernel-initiated
  turns (§9, draft). v0 is superseded and no longer served.
- A status and versioning section: the Draft, Stable, Deprecated and Final
  states, and how a conformance-passing kernel can claim v1.

### Added

- The kernel (`sage/`), standard library only: journal, policy, disposition,
  the HTTP device listener and the Ollama `/api/chat` adapter.
- The black-box conformance suite (`conformance/`), unit tests (`tests/`) and
  an opt-in eval against a real model (`evals/`, written fixtures; live ones
  stay local in `evals/local/`).
- Docker and Make for every task; CI running lint, the dependency check,
  a secret scan, the unit tests and the conformance suite.
- Apache-2.0 license, README, CONTRIBUTING, SECURITY, Code of Conduct
  (Contributor Covenant 3.0), issue and pull request templates.

[Unreleased]: https://github.com/1xabhay/sage-kernel/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/1xabhay/sage-kernel/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/1xabhay/sage-kernel/releases/tag/v0.1.0
