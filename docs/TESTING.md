# Testing

Every test runs on throwaway state. None of them talks to the live kernel, the robot or a GPU,
except `make eval`, which is opt-in and needs Ollama.

## Kernel (repo root)

```sh
make test           # unit tests (tests/), then the conformance suite (conformance/), in Docker, Python 3.12
make check          # the same, under its old name
make unit           # unit tests only
make conformance    # the conformance suite only
make conformance-device   # a body against this kernel: the text body, or SAGE_DEVICE_CMD="<command>"
make source         # the source-tree checks, on the host (they skip inside the image, which ships without the tree)
make lint           # ruff, in Docker, code streamed in
make vuln           # fails the day the kernel gains a third-party dependency
make secrets        # gitleaks over the whole history, offline
make hygiene        # what CI runs: source, docs, lint, vuln, secrets, test
```

- Docker runs from the compose project `sage-kernel-test-<lane>` (`compose.test.yaml`): no volumes,
  no ports, one project per worktree.

- Another kernel, or fast iteration on the host: `SAGE_KERNEL_CMD="python3 -m sage" make conformance`
  starts that command as a subprocess with a fake Ollama.
- `make eval` (serial lane): replays live conversations from the gitignored `evals/local/`
  through the kernel against the host's Ollama. `SAGE_MODEL=phi4-mini make eval` picks the model.

## Reachy Mini (devices/reachy-mini)

```sh
make -C devices/reachy-mini test                                 # the testbench and the Makefile conventions (devices/reachy-mini/tests)
make -C devices/reachy-mini test-all                             # that, each app, integration, robot and research: all that loads no speech model
make -C devices/common/sage_device test                          # the body-agnostic device package
make -C devices/reachy-mini/apps/reachy_sage_voice test          # the voice app, no robot
make -C devices/reachy-mini/apps/reachy_sage_voice integration   # the voice against the real kernel on a free loopback port
make -C devices/reachy-mini/apps/reachy_sage_voice/robot test    # what is installed on the robot: health logger, guard, journal (stdlib)
make -C devices/reachy-mini/apps/reachy_sage_voice/services test # STT, TTS and research contract tests, native: loads the real speech models
make -C devices/reachy-mini/apps/reachy_sage_voice/services test-research # research alone: the web and Shazam are fakes, no model
make -C devices/reachy-mini/apps/reachy_sage_voice/services test-docker   # all three again, in the sealed images
make -C devices/reachy-mini/apps/reachy_sim test                 # the MuJoCo sim, headless (2-3 min)
make -C devices/reachy-mini/apps/reachy_sim lint
```

CI (`.github/workflows/reachy.yml`, on pull requests and on pushes to `main` and `live`) runs
`make lint imports vuln test` for the workspace, `sage_device`, the voice app and the sim, plus the
voice app's `integration`, the robot tests, and the services' `vuln` and `test-research`.
`devices/reachy-mini/tests/test_conventions.py` fails if a project under `devices/` drops out.

Run on the Mac only, never in CI:

- The STT and TTS contract tests (`services test`): they load Parakeet and Pocket from the
  Hugging Face cache in `services/.local/hf`, fetched on the first run. `test_only_azelma_is_baked_in`
  runs whenever `HF_HOME` is set, as `make` sets it.
- `services test-docker`: the only run inside the sealed images, and the only one of the TTS image.
  The services' `deploy` target tests the new STT and research images before it switches to them; the
  TTS runs natively (`pocket-on`) and nothing tests it on the way up.
- `make eval` (kernel) and the voice app's `eval-voice-*` targets: they need a model or recordings.

## Experiments

```sh
make -C experiments/sage-os test         # cargo tests, in Docker
make -C experiments/music-telling test   # no network, no API key
```

CI (`.github/workflows/experiments.yml`) runs both when `experiments/` changes.

## Docs

```sh
python3 make/docs.py --check   # every relative link and documented make command resolves
python3 make/docs.py           # the checker's own self-test
```

`make docs` calls it, and `make hygiene` and CI call that.

## Everything

`make test-all` fans out to `test` in the kernel, `devices/reachy-mini` and both experiments;
`make hygiene-all` likewise. Neither enters `devices/common/sage_device` or the apps under
`devices/reachy-mini/apps`: `make -C devices/reachy-mini test-all` covers the apps, and
`sage_device` has its own `make test`.
