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

Another pet or another language: copy `sage/character.json`, change it, put it in the `kernel-data` volume, set
`SAGE_CHARACTER=/data/character.json` in `.env`, then `make up`. The file is read once, at start. It must hold
every key the default holds, and each pattern must be a regular expression; a missing or wrong one stops the
start, and `docker logs` names the file and the key. Unset, the kernel is Sage, and its prompts are byte for byte what they were.

## The whole of Sage, on and off

On the owner's Mac, where the kernel, the voice's services, the TTS and the voice were each
deployed once:

```sh
make sage-down   # backs up the journal, turns the jobs off, stops the containers and frees the model
make sage-up     # starts the VM and the containers, waits until they are healthy, then loads the jobs
```

Neither builds, deploys or removes anything. After `sage-down`, Sage stays down across a reboot;
after `sage-up`, a reboot brings it back (the `ai.sage.up` login job runs `make sage-up`).

## Journal

The journal is the agent's memory: the `kernel-data` volume of the compose project `sage-kernel`.
`make down` keeps it. Never `down -v`, never remove the volume, never point a test at it.

- `make backup` writes a read-only, integrity-checked copy into the gitignored `.backups/`
  (mode 600). `make deploy` and `make sage-down` run it first, and so does the nightly
  `ai.sage.maintain` job.
- What Sage remembers is in the journal's checkpoint, and its owner reads and fixes it while it runs:
  `docker exec sage-kernel-kernel-1 python -m sage memory list` (then `forget ID`, `edit ID "text"`,
  `add "text"`). Each record shows the line it came from; a change is journaled as the owner's.
- `make restore-drill` proves the newest backup restores, without touching the live kernel: a
  throwaway kernel opens a copy and answers a new line. The nightly job runs it after the backup.
- `make restore BACKUP=.backups/journal-<time>.sqlite3`, after `make sage-down`, puts a backup
  into the live volume. The journal it replaces is kept in the volume as
  `kernel.sqlite3.before-restore-<time>`; nothing is deleted. `make sage-up` then starts Sage on it.
- Every copy is on this Mac's disk, by the owner's choice (2026-11-17): none leaves the machine.
- The volume's name is pinned in `compose.yaml`.

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
