# Changelog

All notable changes to the Sage kernel and its device protocol are recorded
here. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/);
the package follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: anything may change). The device protocol is versioned on its own,
as an integer (see [docs/PROTOCOL.md](docs/PROTOCOL.md#status-and-versioning)),
and every protocol change is listed under **Protocol**.

## [Unreleased]

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

[Unreleased]: https://github.com/1xabhay/sage-kernel/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/1xabhay/sage-kernel/releases/tag/v0.1.0
