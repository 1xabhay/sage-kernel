# Source review and critique

Reviewed October 13, 2026. Sources are primary project documentation/source,
original engineering articles, or the authors' research paper. “Documented”
means the upstream source describes behavior. It does not mean tested here.
The local execution record is separate in [EVIDENCE.md](EVIDENCE.md).

## Keep, tighten, and challenge

| Spec decision | Assessment | Consequence |
| --- | --- | --- |
| Distribution of existing projects | Keep: ownership is clear and avoids a second runtime | Custom code concentrates on controls, policy, integration, and packaging |
| OpenClaw + existing Pipecat connector | Keep provisionally: a connector exists; release compatibility remains unproved | M2 proves actual methods before calling versions supported |
| Browser before CoreS3 | Keep and strengthen | Hardware must not hide runtime or permissions failures |
| Press-to-talk | Keep | Explicit capture, simpler first audio path; verify echo/noise on hardware later |
| Native memory | Tighten: persistence and precise user-controlled notes are different requirements | Probe bounded note CRUD and deletion before assuming native recall satisfies P4/P8 |
| Native reminders | Keep | Scheduler owns execution; delivery to our endpoint needs separate wiring |
| Provider independence | Keep supported-profile definition | Include recall/embedding settings and speech settings in continuity tests |
| Six states | Keep for presentation | Track run outcome and transport separately; cancellation can be pending while display is idle |
| Permissions after firmware | Change sequence | Enforce the restricted profile before a meaningful product proof |
| Thirty-turn p95 | Keep as a preliminary gate | Report failures and uncertainty; do not claim a stable performance guarantee |

## Findings that change implementation

**The connector supplies runs, not a complete voice conversation.** Pipecat's
gateway service accepts send/steer/abort frames and produces run events. Its
documentation explicitly assigns spoken-turn behavior to its caller. Its stop
and cancel lifecycle stops active work. Reuse it; write only the missing turn
presentation and stop mapping. [Gateway service](https://reference-server.pipecat.ai/en/latest/api/pipecat.services.openclaw.gateway.html)

**Reconnect is not replay or durable exactly-once behavior.** The reviewed client
targets OpenClaw v2026.6.1, negotiates protocol 4, uses a new UUID as the
idempotency key for each `start`, and fails an in-flight stream on socket loss.
An abort response of false can mean unknown or already finished. Our inference:
calling `start` again is a new request and cannot establish safe write recovery.
Record correlation and inspect durable evidence; keep unknown when evidence is
insufficient. [Client source](https://reference-server.pipecat.ai/en/stable/_modules/pipecat/services/openclaw/client.html)

**Matching protocol numbers are necessary but insufficient.** Current OpenClaw
docs describe protocol 4 for operators. Negotiation does not establish that the
chosen connector version matches auth, method payloads, events, or result
semantics. Pin tested artifacts together; never silently combine “stable”,
“latest”, and firmware main-branch docs as a release. [Protocol versioning](https://docs.openclaw.ai/gateway/protocol/versioning)

**Explicit-only memory is a configuration requirement.** Current OpenClaw docs
describe Markdown memory, recall tools, default-on pre-compaction saving, and
default-on dreaming/consolidation. Recall can use a separately configured
embedding provider. Disable applicable automatic write paths for this starter
profile, inspect hooks, and test writes on the selected release. Provider choice
includes this extra model/service boundary. [Memory overview](https://docs.openclaw.ai/concepts/memory)

**“Allow memory” is not a bounded note write capability.** The documented memory
group contains search/get; filesystem tools include write/edit. Current local
onboarding defaults to a full tool profile when none is configured. Our
inference: a starter skill plus recall allowlist does not prove writable notes
with safe path limits. Set explicit effective permissions, inspect exposed
tools, and attempt forbidden writes. Use a tiny bounded runtime tool only if
native policy cannot meet the contract. [Tool policy](https://docs.openclaw.ai/gateway/config-tools/tool-policy)

**Delete must name the surfaces it covers.** Upstream memory-forget uses source
lineage; its docs expressly exclude original transcripts, all freeform edits,
and external copies from general erasure. Our note deletion must verify active
recall and in-session context separately. Disable automatic re-ingestion paths
for the explicit-only profile; retained backups/transcripts need a stated
retention boundary. [Memory provenance and deletion](https://docs.openclaw.ai/concepts/memory-provenance)

**Scheduling and delivery are different facts.** OpenClaw documents persistent
jobs and delivery to a channel, webhook, or nowhere. A device's absence does
not mean the scheduler failed; a recorded run does not prove the owner heard
it. Add endpoint delivery/acknowledgment only at the actual integration slice.
[Automations](https://docs.openclaw.ai/automation/cron-jobs)

**A session activity read can inform admission, not prove tool success.** Current
session-control docs expose aggregate active-run state and exact IDs when
available. Such observations can help avoid cross-endpoint interruption. They
do not establish a completed note write or reminder creation. Use exact IDs
and inspect relevant recorded results. [Session control](https://docs.openclaw.ai/gateway/protocol/rpc-session-control)

**Two containers are not automatically one loopback interface.** Host installs
normally bind the gateway to loopback; upstream notes different container
defaults. Use a private application network and server-side gateway credentials,
then test access explicitly. The single-owner trust model fits v0.1; it is not
adversarial tenant isolation. [Security model and defaults](https://docs.openclaw.ai/gateway/security)

**The browser path can reuse a development runner.** SmallWebRTC docs offer an
existing runner/web interface, signaling, STUN/TURN configuration, and
application messages. Reuse that browser proof before creating a web app.
Cross-network tests need relay fallback; macOS/container networking can require
it even during development. [SmallWebRTC](https://docs.pipecat.ai/api-reference/server/services/transport/small-webrtc)

**Supported board is not finished onboarding.** The ESP32 repository documents
CoreS3/BOX-3 support, a recursive checkout, ESP-IDF, and environment-configured
Wi-Fi and offer URL. Build-time configuration is useful for the first local
prototype. Authentication, revocation, and portable setup still need proof.
[ESP32 SDK](https://github.com/pipecat-ai/pipecat-esp32)

**Backup integrity is not operational restoration.** Current native backup docs
describe state/workspace archives, safe SQLite capture, verification, and
sensitive credentials. Do not copy live database files or infer restoration
from archive verification alone. Restore into an isolated target with delivery
disabled, verify product behavior, then transfer active ownership.
[Backup documentation](https://docs.openclaw.ai/install/backups)

## Compare architectural approaches

| Approach | Strength | Cost / limitation | Decision |
| --- | --- | --- | --- |
| Small functional core + upstream imperative services | Deterministic local rules; existing media/agent work reused | Real boundary checks still essential | Chosen |
| Pure functional rewrite of all services | Uniform abstractions | Reimplements transport, persistence, scheduling, providers | No requirement justifies it |
| Entire product as callback-driven glue | Quick first demo | State, stop, and reconnection rules scatter across callbacks | Keep callbacks in thin adapters; centralize policy |
| Integrated remote speech-to-speech model | Potentially shorter voice path | Could couple speech/reasoning choices; tool/state ownership must be proved | Evaluate only if measured voice latency warrants it |
| Full event sourcing / custom durable bus | Replayable history | Duplicates runtime stores and migration burden | Use native evidence; add only a small receipt store if required |
| Xiaozhi / smaller agent runtimes from original spec | Useful fallback candidates | Separate voice, auth, persistence, and migration validation | Re-evaluate only for a failed measured requirement |

This review did not rebuild or benchmark every alternative in the original spec.
Their previously cited claims remain hypotheses, not a new recommendation here.

## Fundamental ideas and testing evidence

Separating pure decisions from imperative I/O follows the original
[functional core / imperative shell explanation](https://www.destroyallsoftware.com/screencasts/catalog/functional-core-imperative-shell).
The proposed boundary is an engineering choice, not an upstream API promise.

AWS's [idempotent API article](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)
explains why retries need an explicit request identity and consistent semantics.
A gateway request ID is not a transaction spanning every agent tool. Our
minimum guarantee is no automatic repeat of uncertain writes, not exactly-once
execution under every failure.

The original [practical test pyramid article](https://martinfowler.com/articles/practical-test-pyramid.html)
supports many cheap tests and fewer costly boundary/end-to-end checks. Use
Python's existing [unittest](https://docs.python.org/3/library/unittest.html)
for the dependency-free core. Live tests inspect results, identity, permissions,
and persistence; they must not compare literal model phrasing.

[WearVox](https://arxiv.org/abs/2601.02391) studies wearable multichannel audio,
noise, and device-directed speech. It motivates noisy/background samples; it
does not predict Pipecat/CoreS3 accuracy or latency. Press-to-talk limits capture
but does not make the included audio intelligible in street noise.

## What we know, and what still requires a test

Known from sources: a Python gateway connector exists; the proposed transport
and reference board exist; the runtime has memory, scheduling, and tool policy.

Not established here: a compatible release tuple, constrained note CRUD,
safe per-endpoint admission across the native UI, end-to-end cancellation,
restart/write reconciliation, provider tool reliability, voice latency,
combined host footprint, battery duration, or portable network behavior.
M2 is intentionally the next gate for the most consequential unknowns.

The subsequent local probe verified the published connector artifact and its
authenticated handshake; see [EVIDENCE.md](EVIDENCE.md). OpenClaw's tested
[2026.10.2 package metadata](https://raw.githubusercontent.com/openclaw/openclaw/v2026.10.2/package.json)
requires Node `>=24.16.0 <25 || >=26.1.0`, so the probe used temporary Node
24.16.0 rather than the installed Node 25. Pipecat's
[1.12.0 project metadata](https://raw.githubusercontent.com/pipecat-ai/pipecat/v1.12.0/pyproject.toml)
requires Python ≥3.11; the actual connector probe used 3.12.13.
