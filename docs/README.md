# Docs

Every doc in the repository, by what it is for.

## Process

- [CONTRIBUTING.md](../CONTRIBUTING.md): layout, naming, setup, branches, lanes, red/green, before a PR, CI, the nevers.
- [TESTING.md](TESTING.md): what runs where; every test on throwaway state.
- [RUNNING-LOCALLY.md](RUNNING-LOCALLY.md): throwaway runs versus the live agent.
- [DEPLOYING.md](DEPLOYING.md): kernel, journal, voice, robot, releases.
- [SECURITY.md](../SECURITY.md), [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md),
  [pull request template](../.github/PULL_REQUEST_TEMPLATE.md).

## Kernel

- [README.md](../README.md#the-kernel): quick start, a first device, writing your own kernel.
- [PROTOCOL.md](PROTOCOL.md): device protocol v1 (draft), the contract. [PROTOCOL-v0.md](PROTOCOL-v0.md): v0, superseded.
- [BLUEPRINT.md](BLUEPRINT.md): the design. [DECISIONS.md](DECISIONS.md): the decision register.
  [ROADMAP.md](ROADMAP.md): what is planned.
- [CHANGELOG.md](../CHANGELOG.md): kernel and protocol changes.
- [bench/LEDGER.md](../bench/LEDGER.md): the voice stack, measured.

## Devices

- [devices/reachy-mini](../devices/reachy-mini/README.md): the workspace, configuration, diagnostics.
- [reachy_sage_voice](../devices/reachy-mini/apps/reachy_sage_voice/README.md): Sage's voice on the robot.
- [reachy_sim](../devices/reachy-mini/apps/reachy_sim/README.md): the MuJoCo simulator.
- [reachy_mini_testbench](../devices/reachy-mini/apps/reachy_mini_testbench/README.md): hardware diagnostics.
- [reachy_mini_pet](../devices/reachy-mini/apps/reachy_mini_pet/README.md): protocol v0, orphaned; to be archived or ported (plan D1).

## Experiments

- [sage-os](../experiments/sage-os/README.md): a Rust kernel; its own [protocol](../experiments/sage-os/docs/PROTOCOL.md).
- [music-telling](../apps/music-telling/README.md): songs told as a story; standalone, moves to `experiments/` with PR #6.

## History (historical)

[history/sage/](history/sage/README.MD): the old kernel, before the rewrite of 2026-10-20. Kept as a
record; its commands were that repository's and do not run here.
