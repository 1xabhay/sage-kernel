# Testing

Every test runs on throwaway state. None of them talks to the live kernel, the robot or a GPU,
except `make eval`, which is opt-in and needs Ollama.

## Kernel (repo root)

```sh
make check          # unit tests (tests/), then the conformance suite (conformance/), in Docker, Python 3.12
make unit           # unit tests only
make conformance    # the conformance suite only
make lint           # ruff, in Docker, code streamed in
make vuln           # fails the day the kernel gains a third-party dependency
make secrets        # gitleaks over the whole history, offline
make hygiene        # lint, vuln, secrets, check
make test           # (coming: repo/guards) the new name for check; check stays as an alias
```

- Docker runs from the compose project `sage-kernel-test` (`compose.test.yaml`): no volumes, no
  ports. Two worktrees running it at once share that project today; per-lane projects
  `sage-kernel-test-<lane>` come with repo/guards.
- The source-tree checks skip inside the image, which ships without the tree. Run them on the host:

```sh
python3 -m unittest tests.test_deploy tests.test_boundary tests.test_docs
```

- Another kernel, or fast iteration on the host: `SAGE_KERNEL_CMD="python3 -m sage" make conformance`
  starts that command as a subprocess with a fake Ollama.
- `make eval` (serial lane): replays live conversations from the gitignored `evals/local/`
  through the kernel against the host's Ollama. `SAGE_MODEL=phi4-mini make eval` picks the model.

## Reachy Mini (devices/reachy-mini)

```sh
make -C devices/reachy-mini/apps/reachy_sage_voice test          # the voice app, no robot
make -C devices/reachy-mini/apps/reachy_sage_voice integration   # the voice against the real kernel on a free loopback port
make -C devices/reachy-mini/apps/reachy_sage_voice/services test # STT and TTS contract tests, native
make -C devices/reachy-mini/apps/reachy_sage_voice/services test-docker   # the same, in the sealed images
make -C devices/reachy-mini/apps/reachy_sim test                 # the MuJoCo sim, headless (2-3 min)
make -C devices/reachy-mini/apps/reachy_sim lint
```

The workspace tests (testbench and pet, under `devices/reachy-mini/tests`) have no make target
yet and are not in CI (coming: plan C2, a `devices/reachy-mini/Makefile`).

## Experiments

```sh
make -C experiments/sage-os test   # cargo tests, in Docker
```

music-telling's targets move with it to `experiments/music-telling` (PR #6).

## Docs

```sh
python3 make/docs.py --check   # every relative link and documented make command resolves
python3 make/docs.py           # the checker's own self-test
```

`make docs` will call it (coming: repo/guards), and so will `make hygiene` and CI (coming: plan C3).

## Everything

`make test-all` fans out to every project's `test` (coming: plan C1). Until then, run the
blocks above for what you touched.
