# Local and future cloud package

Historical gateway package. The active companion now uses `compose.yaml` and
localhost:8765; see [FUNDAMENTALS.md](FUNDAMENTALS.md). To deliberately run the
earlier workflow below, use `compose.reference.yaml` and the `reference-*`
Makefile targets (including `reference-token`, `reference-devices`,
`reference-approve`, and `reference-backup`). Its saved volumes remain intact.

Contract: package the implemented policy, tests, documents, and pinned runtime
probe. Use the same Compose file locally and on one Linux VM. State survives
container recreation; setup preserves an existing config; source archives and
images contain no generated credentials or personal state. The first package
does not implement the voice application or the remaining product gates.

## Start and check locally

Install Docker with Compose v2+ and `make`, then run from this directory:

```sh
make reference-up
make reference-check
```

`make reference-up` builds the Python runner, initializes state once, starts the gateway,
and waits for readiness. `make reference-check` runs the policy/setup suite and the real
Pipecat gateway-authentication tests. No model request is sent.

Open [the local Control UI](http://127.0.0.1:18791/). Retrieve its generated
credential privately in your own terminal:

```sh
make reference-token
```

Enter it in the Control UI connection settings. The first browser connection
may also require native device pairing. Inspect pending requests and approve
your browser's exact request ID:

```sh
make reference-devices
make reference-approve DEVICE_ID=<request-id>
```

The Control UI is the upstream administration interface. The Sage press-to-talk
browser app remains a later milestone. Configure a model provider before
attempting a model conversation. This starter denies agent tools and disables
scheduled jobs and automatic memory writes; defining the bounded capability
profile is still required before enabling notes/reminders/search.

```sh
docker compose -f compose.reference.yaml ps
docker compose -f compose.reference.yaml logs -f gateway
make reference-down
```

`make reference-down` removes containers and retains state. `restart: unless-stopped`
keeps the gateway running after a Docker-engine restart unless explicitly stopped.
Do not use `docker compose down -v` for routine shutdown: it deletes the named
state volume. Keep the same Compose project name when restarting an instance.

## What is packaged

| Artifact | Purpose |
| --- | --- |
| `compose.reference.yaml` | Same gateway/bootstrap/test workflow on local Docker and a Linux VM |
| `Dockerfile.reference` | Non-root Python test image; installs the hashed dependency lock |
| `requirements.lock` | Exact connector dependency versions and accepted artifact hashes |
| `pyproject.toml` | Installable Python core; Pipecat is the optional `gateway` extra |
| `dist/sage_companion-0.1.0-py3-none-any.whl` | Dependency-free core wheel |
| `dist/sage_companion-0.1.0.tar.gz` | Python source distribution |
| `dist/sage-companion-0.1.0.tar.gz` | Complete deployment source kit, documents, tests, and specification |

The listed v0.1 artifacts are historical. Current `make package` builds the v0.2
companion kit; `make wheel` builds its Python wheel/source
distribution with `make wheel` (requires `uv`). Run `make` or `make help` to list
all reusable commands; plain `make` displays help and does not start services.
Wheel installation does not start the gateway:

```sh
python3 -m pip install dist/sage_companion-0.1.0-py3-none-any.whl
```

The deployment archive includes only listed project sources, excluding bytecode.
The Docker build context also uses an explicit allowlist. State lives in the
`sage-companion_companion-state` Docker volume; credentials are generated there,
not baked into an image, archive, or checked-in environment file. The test runner
mounts this volume read-only and shares the gateway's network namespace.

The official OpenClaw 2026.10.2 image and Python 3.12.13 base image are pinned by
multi-architecture digest. Both advertise Linux ARM64 and AMD64 manifests.
The local test verifies ARM64; AMD64 and a real cloud host remain untested.
The lock verifies runtime distributions. Source distributions such as docopt may
invoke build tooling during installation; this is not an air-gapped or
bit-for-bit reproducible image build.

## Run later on a cloud Linux VM

Use one owner-controlled VM with Docker Engine and Compose. Provisioning a VM,
DNS, and paid services is outside this package; no cloud resources were created.
Start with the existing planning allocation of 2 CPU cores and 4 GB RAM, then
measure the real workload. Leave disk space for images, state, logs, and backups.
This allocation is not a verified minimum for voice or local inference.

1. Transfer `dist/sage-companion-0.1.0.tar.gz` to the VM using SSH/SCP.
2. Extract into a new directory, so an existing deployment is not overwritten:

   ```sh
   mkdir sage-companion
   tar -xzf sage-companion-0.1.0.tar.gz -C sage-companion
   cd sage-companion
   make reference-up
   make reference-check
   ```

3. From your laptop, open a tunnel to the VM:

   Stop the local gateway with `make reference-down` first if it already occupies port
   18791. Use the same local URL so the configured browser origin remains valid.

   ```sh
   ssh -N -L 18791:127.0.0.1:18791 user@your-vm
   ```

4. Open `http://127.0.0.1:18791/`, then retrieve the VM's generated credential
   and pair your browser using the same commands as local setup.

The gateway binds to the container interface so Docker can forward traffic,
while its published host port binds only to `127.0.0.1`. Keep that mapping and
the VM firewall unchanged for the SSH workflow. A public browser/device endpoint
requires a separately configured HTTPS/authentication boundary; the future voice
path also needs tested signaling and STUN/TURN. Do not expose the gateway port
directly to the Internet merely to make a phone connect.

A new VM gets a new companion state and credential. Moving the same companion
requires a verified private backup and a single active state owner, not copying
the source kit. To back up through the native runtime:

```sh
make reference-backup
```

The backup destination is `/backups` in the separate private
`sage-companion_companion-backups` Docker volume. OpenClaw rejects a destination
inside the state/workspace being backed up. Setup makes the backup directory
private and writable by the gateway's UID 1000. `make reference-down` retains both volumes.
Keeping backups on the same laptop verifies backup creation but does not protect
against loss of that laptop; transfer them privately before relying on recovery.

Follow the pinned runtime's restore procedure, transfer backups privately, and
keep reminder delivery disabled on a restore target until ownership transfers.
Native archive verification has passed locally. It reported that embedded
runtime SQLite files under `codex-home` were archived as opaque bytes without
live snapshots or integrity checks. This package has not yet passed the complete
personal-backup restore gate; do not treat manifest verification as proof that
every embedded runtime database will restore consistently.

Local Docker runs the same package inside Linux containers on your laptop,
so boot, authentication, persistence, and native backup can be checked without
a cloud account. The tunnel target is for later use with an existing VM; it
does not provision or deploy a cloud server. Cloud commands also need `make`
installed on that VM.

## Evidence and source guidance

See [EVIDENCE.md](EVIDENCE.md) for actual local results. Compose readiness and
tool profiles follow the [Docker startup-order documentation](https://docs.docker.com/compose/how-tos/startup-order/)
and [Compose profiles documentation](https://docs.docker.com/compose/how-tos/profiles/).
The runtime image, persistent directory mounts, pairing, and native CLI operations
follow [OpenClaw's Docker guide](https://docs.openclaw.ai/install/docker).
