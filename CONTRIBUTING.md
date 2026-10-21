# Contributing

Thank you for helping. Sage is small on purpose: a stdlib-only core, one
device protocol, and a conformance suite that is the contract. Please read
[docs/BLUEPRINT.md](docs/BLUEPRINT.md) and [docs/PROTOCOL.md](docs/PROTOCOL.md)
before a large change, and open an issue first to discuss it.

## Setup

You need Docker with Compose v2 and `make`. Everything runs in containers:

```sh
make check     # unit tests (tests/), then the conformance suite (conformance/)
make lint      # ruff
make hygiene   # lint, vuln, secrets and check: what CI runs on every pull request
```

Tests run in their own compose project with throwaway state, never against a
running kernel. Do not run `make up` or `make down` to test.

## Test first

Every change to behaviour starts with a failing test.

1. Commit the test that specifies the behaviour, failing (`... (red)`).
2. Commit the smallest change that makes it pass (`... (green)`).
3. `make check` passes on every green commit.

A change to the device protocol changes [docs/PROTOCOL.md](docs/PROTOCOL.md)
and `conformance/` together, in the same pull request, and gets an entry in
[CHANGELOG.md](CHANGELOG.md). The suite talks to the kernel over HTTP only:
never import kernel code from it.

## Rules of the codebase

- The core (`sage/`) imports only the standard library and itself; anything
  else lives in `sage/adapters/` (`tests/test_boundary.py` enforces it).
  New third-party dependencies need an issue and a measured reason first.
- Containers stay sealed: loopback-only ports, read-only, unprivileged
  (`tests/test_deploy.py`).
- Never commit recordings or transcripts of real people. Live conversations
  for `make eval` go in `evals/local/`, which is gitignored; the tracked
  fixtures are written.
- Never commit `.env` or a device token.

## Pull requests

Keep them small and focused, describe what changed and why, and say how you
tested it. By contributing you agree that your contribution is licensed under
the [Apache License 2.0](LICENSE) (its section 5), and you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Report security issues privately, as
[SECURITY.md](SECURITY.md) describes, not in an issue.
