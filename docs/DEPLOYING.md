# Deploying

All of this is the serial lane: from the main checkout, on `main`, a clean tree, the owner aware.
Most of the targets are still being built; each says where. What is true today is marked **Today**.

## Kernel

**Today:** `make up` builds the image from the checkout and starts it on `127.0.0.1:8776`,
waiting for the health check. Deploying is: be on the commit you want, then

```sh
make up
```

Rollback today is the same from the previous good commit. The journal is untouched either way.

- `make deploy` (coming: plan C4): an image per SHA built from `git archive`, that image tested, a journal backup,
  up without `--build`, a mounts check, kernel and robot status, the SHA logged.
- Rollback: deploy the previous SHA's image, no rebuild (coming: plan C4).

## Journal

The journal is the agent's memory: the `kernel-data` volume of the compose project `sage-kernel`.
`make down` keeps it. Never `down -v`, never remove the volume, never point a test at it.

- **Today:** there is no backup target. Before anything that touches the volume, the owner takes
  a copy by hand into the gitignored `.backups/` (plan WP1 records the baseline: integrity check,
  row count, last sequence, mounts).
- `make backup` (coming: plan C2), run by `make deploy` before every deploy (coming: plan C4).
- The volume's name pinned in `compose.yaml` (coming: repo/guards).

## Voice (on the Mac)

The voice runs on the Mac, not on the robot; it reaches the robot at `<robot-host>`.

**Today:** by hand, in a terminal, from the main checkout:

```sh
make -C devices/reachy-mini/apps/reachy_sage_voice/services up   # STT and TTS
make -C devices/reachy-mini/apps/reachy_sage_voice run
```

`make deploy` in the voice app (coming: plan C5): the tests at the SHA, a preflight, a
`git archive` export into the gitignored `.deploy/voice/<sha>`, run by a launchd LaunchAgent
(KeepAlive, 10 s throttle) with its env from the voice's config folder outside the repo; the SHA
logged. Rollback: point the agent at the previous export.

## Robot

The robot's firewall allows SSH and the daemon only from the Mac. Changes to it run with a 90 s
rollback timer, so a mistake cannot lock the robot out.

Coming (plan B3, D6): the tracked rules `include` a hosts file that lives only on the robot,
written by `make -C devices/reachy-mini/robot allow-this-mac` (coming: plan B3); LAN SSH only
once key-only sshd is confirmed. Never write the robot's or the Mac's address in a tracked file: `<robot-host>`,
`<mac-host>`.

## Releases

| Project | Tag | What a release is |
| --- | --- | --- |
| Kernel | `kernel/vX.Y.Z` | the tag equals `pyproject.toml`'s version, CI green at that SHA, a [CHANGELOG](../CHANGELOG.md) entry |
| Voice | `voice/vX.Y.Z` | a publish of the app to its Hugging Face Space: distribution only; the robot's voice runs from the Mac, above |

- **Today:** kernel releases were tagged `v0.1.0` and `v0.2.0` (the old scheme); the Space was
  uploaded by hand.
- `make/release_check.py` checks tag, version and CI (coming: plan C4).
- `make release` in the voice app (coming: plan C5) publishes an allow-listed file set to the
  Space after a hygiene scan, with `DRY_RUN`.
