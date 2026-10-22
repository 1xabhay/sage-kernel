# Changelog

All notable changes to the Sage kernel and its device protocol are recorded
here. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/);
the package follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: anything may change). The device protocol is versioned on its own,
as an integer (see [docs/PROTOCOL.md](docs/PROTOCOL.md#status-and-versioning)),
and every protocol change is listed under **Protocol**.

## [Unreleased]

### Added

- Notes: a device can hand the kernel a topic and up to 64 passages (such as a
  song looked up on the web); each turn, the 3 that best match the newest line
  ride on the newest message, after the first, as information. Journaled and checkpointed.

### Protocol

- New event `notes` `{topic, passages}` (§10, draft). JSON bodies may be up to
  131072 bytes (was 8192).

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
