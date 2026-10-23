# Sage kernel

[![CI](https://github.com/1xabhay/sage-kernel/actions/workflows/ci.yml/badge.svg)](https://github.com/1xabhay/sage-kernel/actions/workflows/ci.yml)

A minimal, model-independent, dependency-free kernel for always-on agents.
Bodies and tools are plugins on one HTTP device protocol; the kernel owns
identity, memory, the journal and authorization, and never gets a shell of its
own. [Reachy Mini](https://github.com/pollen-robotics/reachy_mini) is its first
device.

- **Protocol:** [docs/PROTOCOL.md](docs/PROTOCOL.md), device protocol v1 (**draft**).
- **Conformance:** [conformance/](conformance/), a black-box HTTP suite any kernel must pass unchanged.
- **Core:** [sage/](sage/), Python 3.12 standard library only.
- **Design:** [docs/BLUEPRINT.md](docs/BLUEPRINT.md), [docs/DECISIONS.md](docs/DECISIONS.md),
  [docs/ROADMAP.md](docs/ROADMAP.md). Changes: [CHANGELOG.md](CHANGELOG.md).
- **Devices:** [devices/reachy-mini/](devices/reachy-mini/), the Reachy Mini voice app, pet and simulator.
- **Apps:** [apps/](apps/), other programs built on Sage. **Experiments:** [experiments/](experiments/), e.g. the Rust `sage-os`.

## Quick start

You need Docker with Compose v2, `make`, and [Ollama](https://ollama.com) on the
same machine. No GPU is needed for the tests.

```sh
ollama pull qwen3:1.7b                     # the default live-turn model (SAGE_MODEL)
cp .env.example .env && chmod 600 .env     # then set SAGE_DEVICES, see below
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # a token for your device
make up                                    # the kernel, on 127.0.0.1:8776 only
```

`SAGE_DEVICES` is `name=token,...`: one token of at least 32 random characters
per device. Issuing a token is your consent for that device. The kernel reaches
Ollama at `SAGE_MODEL_URL` (default `http://host.docker.internal:11434`, the host
as seen from the container). `make down` stops it; its journal stays in the
`kernel-data` volume.

## A first device

A device is anything that can speak HTTP: it says hello with its manifest,
posts events and long-polls for effects. With the device's token in
`SAGE_TOKEN`:

```sh
K=http://127.0.0.1:8776; A="Authorization: Bearer $SAGE_TOKEN"; J="Content-Type: application/json"
curl -s -H "$A" -H "$J" $K/v1/hello -d '{"protocols": [1], "manifest": {"senses": ["speech"], "tools": []}}'
# -> {"protocol": 1, "sequence": N}
curl -s -H "$A" -H "$J" $K/v1/events -d '{"kind": "submitted", "turn_id": "t1", "text": "Hello!"}'
curl -s -H "$A" "$K/v1/effects?since=N&wait=20"
# -> a reply_delta while it streams, then {"sequence": S, "kind": "reply", "text": "...", ...}
curl -s -H "$A" -H "$J" $K/v1/events -d '{"kind": "result", "sequence": S, "status": "done"}'
```

Every delivered effect gets one `result`. Tools (an `emote`, a move) are
declared in the manifest, shaped like an MCP tool list. The full contract,
including streaming, Stop, timing and errors, is in [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Tests

```sh
make check         # unit tests, then the conformance suite, in Docker (Python 3.12)
make lint          # ruff, in Docker
make hygiene       # lint, vuln, secrets and check: what CI runs
```

Tests never touch the live kernel: they run in their own compose project
(`compose.test.yaml`) with throwaway state. `make eval` replays conversations
through the kernel against a real Ollama model and is opt-in.

## Writing your own kernel

The conformance suite starts any kernel as a subprocess, configures it only
through environment variables, and gives it a fake Ollama. To test yours:

```sh
SAGE_KERNEL_CMD="/path/to/your-kernel" make conformance
```

A kernel that passes the unchanged suite may claim compatibility with device
protocol v1 as described in [docs/PROTOCOL.md](docs/PROTOCOL.md#status-and-versioning).
Device authors: the rules a device can rely on are in §8; a device-side suite
is on the [roadmap](docs/ROADMAP.md).

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) (test-first, `make check`),
[SECURITY.md](SECURITY.md) (how to report a vulnerability) and the
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

[Apache License 2.0](LICENSE). Copyright 2026 The Sage kernel authors; see [NOTICE](NOTICE).
