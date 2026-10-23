# Contributing

One repository, several projects. Every process here is a `make` target; if a
step has no target yet, it says which lane or plan item is building it.
`python3 make/docs.py --check` keeps this file honest (a `make docs` target
comes with the guards lane, repo/guards).

## Layout

| Path | Project | Kind |
| --- | --- | --- |
| `/` (`sage/`, `tests/`, `conformance/`, `evals/`) | the Python kernel; moves to `kernel/` last (plan WP E) | Docker, stdlib only |
| `devices/reachy-mini/` | Reachy Mini workspace: `apps/reachy_sage_voice`, `apps/reachy_sim`, `apps/reachy_mini_testbench` | uv |
| `experiments/sage-os/` | Rust kernel experiment | Docker, cargo |
| `experiments/music-telling/` | songs-as-story CLI, standalone (after PR #6; today `apps/music-telling/`) | uv |
| `docs/` | process and kernel docs; `docs/history/` is the old kernel, historical | [index](docs/README.md) |
| `bench/` | measurements and `bench/LEDGER.md` | |

## Naming

- Folders: a group is one word (`devices`, `experiments`); a project is kebab-case
  (`reachy-mini`, `sage-os`); a Reachy Mini app is a snake_case package (`reachy_sage_voice`, Pollen's convention).
- Environment: one prefix per project: `SAGE_` kernel, `SAGE_VOICE_`, `SAGE_OS_`,
  `MUSIC_TELLING_`, `TESTBENCH_`. `REACHY_MINI_*` belongs to Pollen's SDK. A client of the
  kernel uses `<PREFIX>_KERNEL_URL` and `<PREFIX>_KERNEL_TOKEN`. Real addresses live only in a gitignored `.env`.
- Compose: every file pins `name:`, every volume pins `name:`; test projects are
  `<name>-test-<lane>`; images are `<name>:<sha>`.
- Branches: see below. Tags: `<project>/vX.Y.Z`.
- Standard targets in every project: `test lint imports vuln secrets up down hygiene`,
  plus `run status deploy backup release` where they apply (coming: plan C1/C2; a
  conventions checker lists the gaps).

## Setup

```sh
docker context show     # must print colima: Docker runs in the Colima VM
make hooks              # (coming: repo/guards) pre-push and commit-msg hooks
```

Docker with Compose v2, `make`, and [uv](https://docs.astral.sh/uv/) for the uv projects.
Ollama on the host for `make eval` and the live kernel only; the tests need no model and no GPU.

## Branches

- Pushable: `<project>/<topic>`, project one of `kernel voice sim devices sage-os music repo`.
  Branch from `origin/main`; a PR goes to `main`.
- Local-only, never pushed: `history/*`, `reachy/*`, `backup/*`, `private-history`.
  They hold private history (addresses, transcripts). The pre-push hook refuses them
  (coming: repo/guards); until then, push by explicit name only: `git push origin <project>/<topic>`.

## Two lanes

- **Parallel lane (CPU):** each piece of work in its own worktree under
  `.claude/worktrees/<name>`, on its own branch. Tests there use throwaway state in their
  own compose project. Never `make up` or `make down` from a worktree; they act on the live
  kernel (they refuse in a worktree once repo/guards lands).
- **Serial lane:** anything that needs the GPU, the robot or the owner, one at a time, from
  the main checkout: model runs (`make eval`), the robot, the live kernel, deploys, releases.

## Test first

1. Commit the failing test that specifies the behaviour. Its subject ends `(red)`.
2. The next commit makes it pass, with the smallest change.
3. Every green commit passes the nearest project's tests; the commit-msg hook runs them
   (coming: repo/guards).

What runs where: [docs/TESTING.md](docs/TESTING.md).

## Before a PR

```sh
make hygiene                  # kernel: lint, vuln, secrets, unit and conformance, as CI
python3 -m unittest tests.test_deploy tests.test_boundary tests.test_docs   # source-tree checks
python3 make/docs.py --check  # every link and documented make command resolves
make -C devices/reachy-mini/apps/reachy_sage_voice test   # if you touched the voice
make -C devices/reachy-mini/apps/reachy_sim test          # if you touched the sim
```

`make test-all` will fan out to every project (coming: plan C1). Small PRs; say what
changed, why, and how you tested it.

## CI

- `.github/workflows/ci.yml`: the source-tree checks, then `make hygiene`, on every PR and on `main`.
- `.github/workflows/reachy.yml`: the voice's tests, the voice against the real kernel
  (`make -C devices/reachy-mini/apps/reachy_sage_voice integration`) and the sim's tests,
  on every PR and on `main`.
- Coming (plan C3): an unfiltered required `repo` job, path-filtered project jobs, a job per experiment.

## Never

- `docker compose down -v`, or removing a volume: the kernel's journal is the agent's memory.
  Back it up first, with the owner ([docs/DEPLOYING.md](docs/DEPLOYING.md#journal)).
- A LAN address, hardware address or home path in a tracked file. Write placeholders
  (`<robot-host>`, `<mac-host>`) and repo-relative paths. `make hygiene` and the pre-push hook
  scan for them (coming: repo/guards).
- Pushing a local-only branch (`history/*`, `reachy/*`, `backup/*`, `private-history`).
- Committing `.env`, a token, or a recording or transcript of a real person
  (live conversations for `make eval` go in the gitignored `evals/local/`).

## Kernel rules

Sage's kernel is small on purpose: a stdlib-only core, one device protocol, and a
conformance suite that is the contract. Read [docs/BLUEPRINT.md](docs/BLUEPRINT.md) and
[docs/PROTOCOL.md](docs/PROTOCOL.md) before a large change, and open an issue first.

- The core (`sage/`) imports only the standard library and itself; anything else lives in
  `sage/adapters/` (`tests/test_boundary.py`). A new third-party dependency needs an issue and
  a measured reason first.
- Containers stay sealed: loopback-only ports, read-only, unprivileged (`tests/test_deploy.py`).
- A protocol change changes [docs/PROTOCOL.md](docs/PROTOCOL.md) and `conformance/` in the same
  PR, with a [CHANGELOG.md](CHANGELOG.md) entry. The suite talks HTTP only: never import kernel
  code from it.

## Licence, conduct, security

By contributing you agree your contribution is licensed under the [Apache License 2.0](LICENSE)
(its section 5) and to follow the [Code of Conduct](CODE_OF_CONDUCT.md). Report security issues
privately, as [SECURITY.md](SECURITY.md) describes.
