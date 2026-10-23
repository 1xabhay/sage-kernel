# Execution evidence

Date: October 13, 2026 (owner timezone). First probe: macOS/Darwin, Apple Silicon.
Later packaging checks: Linux ARM64 containers on Docker Desktop. Full product
release acceptance remains pending. Model, speech, hardware, and noisy-audio
tests were not run.

## M0 and M1

M0 course, research critique, dependency charts, and turn contract are written.
M1 pure policy is implemented in `sage/turn.py` with no third-party dependencies.

TDD evidence: tests were written first. The initial run failed because the module
was absent. A runnable no-op stub then failed the C3 release test with an
assertion that the expected pending-transcript state was absent. Implementation
followed; all 18 policy tests passed on Python 3.12.13 and Python 3.14.7.

Final default command:

```sh
python3 -B -m unittest discover -s tests -v
```

Result: 18 passed, 2 live tests explicitly skipped. Unittest reported 0.001 s;
a measured complete subprocess run including interpreter startup took 0.107 s.
This timing measures test feedback, not voice latency.

Policy coverage: immutable state, transport/display separation, capture release,
single transcript dispatch, stale callbacks, actual playback, playback after
completion, stop-before-abort, cancellation/completion races, unknown outcome
after disconnect, no reconnect replay, and malformed input.

## M2: completed subset

The owner selected a local disposable setup with no configured gateway.
Installed test runtimes live under `/private/tmp`; project source dependencies
and the system Node installation were not changed.

| Artifact | Observed value | What it establishes |
| --- | --- | --- |
| Node | 24.16.0 | Meets the selected OpenClaw package's engine requirement; installed Node 25.9.0 did not |
| OpenClaw | 2026.10.2 (`b8952d4` reported by CLI) | Actual installed gateway starts with isolated state and token auth |
| Python | 3.12.13 | Actual Pipecat connector imports and runs |
| Pipecat | 1.12.0 | Published wheel includes OpenClaw client/frames/gateway modules |
| Pipecat wheel SHA-256 | `4cd3dc071b7b64da7ac700a26a224b773ae6ef8d6694a4566332d2e5e39ed6d9` | Download matched PyPI's published artifact hash |
| Gateway endpoint | `ws://127.0.0.1:18791` | Loopback-only disposable probe |
| Valid auth | Passed through real `OpenClawGatewayClient.connect()` | Actual connector/gateway handshake compatibility |
| Invalid auth | Rejected with token mismatch | Authentication rejection, rather than an unrelated connection error |

The repeatable live command was:

```sh
SAGE_TEST_GATEWAY_CONFIG=/private/tmp/sage-m2-state/openclaw.json \
  /private/tmp/sage-m2-python/bin/python -B -m unittest discover -s tests -p test_gateway.py -v
```

Result: 2 passed in 0.877 s. No model run was sent. This is a tested handshake
tuple, not a supported product release tuple. Gateway state was isolated,
agent tools denied, scheduled jobs disabled, and automatic consolidation/flush
disabled in its config. Their enforcement under a real model turn remains untested.

The first sandboxed startup failed without a usable diagnostic. Running the
disposable gateway with the required local process/network access succeeded.
Its ready log showed approximately 9.26 s startup for that run; this is one
startup observation, not a benchmark. The temporary gateway stopped cleanly
after the probe (399 ms reported for that shutdown). Local documentation links
also passed validation. See [GATEWAY-PROBE.md](GATEWAY-PROBE.md) for setup/re-run.

## Not passed

M2 still needs actual send/abort/result scenarios, restricted note CRUD,
restart persistence, reminder CRUD, and disconnect recovery. There is no chosen
or configured test model provider. M3/M4/M6/M7 remain planned; M5 now has the
packaging subset below. There is no browser voice application, firmware
customization, or voice latency report.

The reducer is in memory. It cannot establish host-crash recovery, endpoint
admission across processes, real local silence timing, authorization, or actual
write outcomes. An unresolved unknown turn currently blocks another turn until
trusted terminal evidence arrives; the product needs an owner-facing recovery
path in M4. Retain this limitation when reviewing or extending the code.

This workspace was not a Git repository when inspected. No commits or PRs
were created. The original spec and SAGE.MD were preserved; README.MD gained
links and the test command above its existing philosophical notes.

## Packaging and local container execution

The owner requested packaging for local use and future cloud deployment.
The first Docker access attempt was blocked when automatic approval review
could not complete because of a usage limit. After the owner reported the limit
restored, Docker access succeeded and work resumed.

Completed artifacts: `compose.yaml`, a Python runner `Dockerfile`, hashed
`requirements.lock`, an installable Python project, a source kit, and
[PACKAGING.md](PACKAGING.md) with the local and Linux-VM workflow. No cloud
resources or provider accounts were created.

| Check | Actual result |
| --- | --- |
| Host Docker | Docker Engine 29.4.0, Compose 5.1.1, Linux ARM64 daemon |
| Image selection | Official OpenClaw 2026.10.2 and Python 3.12.13 bases pinned by multi-architecture digest |
| Gateway runtime | OpenClaw 2026.10.2, image's Node 24.21.0, Linux ARM64 |
| Dependency install | Python runtime distributions installed with exact versions and required hashes |
| Setup policy tests | Four passed: private state, independent credentials, preservation of operator edits, rejection of damaged config |
| Host suite | 22 passed; 2 live tests intentionally skipped |
| Container suite | All 24 passed, including the two real gateway-auth tests, in 0.794 s |
| Local UI | HTTP 200 at `http://127.0.0.1:18791/` |
| Host exposure | Published Docker port binds only to `127.0.0.1:18791` |
| Persistence | Removed/recreated containers without deleting volume; config fingerprint, credential, and workspace marker survived; marker removed afterward |
| Readiness after recreation | `/startupz` returned HTTP 200; gateway healthy |
| Python wheel | Built and installed in an isolated environment; import/transition asserted outside source checkout |
| Deployment archive | Source allowlist; validated safe relative paths and exclusion of generated credentials/state/bytecode |

The local gateway is left running for the owner. Shut down with `make down`,
which preserves the state volume. `make up` starts it again; `make check` runs
the container suite. Native auth-token/pairing commands are in the packaging
guide; the generated token was never printed into this chat or included in
the source archives.

One observation shortly after startup: gateway memory 1.21 GiB and CPU 4.01%;
image sizes approximately 1.26 GB for the gateway and 217 MB for the runner.
These are single observations, not workload benchmarks or cloud sizing guarantees.

Not verified: real cloud host, Linux AMD64 execution, browser pairing interaction,
paid/local model calls, voice, reminder delivery, and full personal-backup restore.
Both base-image indexes advertise ARM64/AMD64, but only ARM64 was run here.
The package proves boot, authentication, basic persistence, and core policy;
it does not complete the remaining product gates.

## Reusable Makefile commands and laptop checks

The owner confirmed testing on the laptop rather than deploying a cloud host.
`make`/`make help` now list startup, container checks, host tests, status, logs,
private token retrieval, device pairing, native backup, wheel/source builds,
deployment packaging, and the future SSH tunnel. Missing device IDs and cloud
hosts fail before executing Docker/SSH; a stub-command self-check verified
argument quoting without contacting a host or approving a real device.

`make test` passed 22 host tests and intentionally skipped the two live tests.
`make check` passed all 24 container tests in 1.241 s before the backup change.
The native backup target initially failed because its output directory was
inside the source state tree. The shared command now uses `/backups`, in a
separate private `sage-companion_companion-backups` volume, initialized for UID
1000. Containers were updated without deleting the existing state volume.

`make backup` then created
`/backups/2026-10-14T01-48-20.599+00-00-openclaw-backup.tar.gz` and native archive
verification passed. The filename reflects the container's UTC clock; the
owner's date was October 13. Embedded `codex-home` SQLite files were reported as
opaque bytes without live snapshots or integrity checks. This is an archive
creation/verification result, not complete personal-backup restoration evidence.
After attaching the backup volume, `make check` passed all 24 container tests
again in 0.825 s. The backup directory's mode was confirmed as 0700, the archive
was present, and `make status` reported the local gateway healthy. Cloud execution
and model/voice behavior remain untested.
