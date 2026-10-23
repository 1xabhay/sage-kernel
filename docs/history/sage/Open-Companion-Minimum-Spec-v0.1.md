# Open Companion — Minimum Specification v0.1

Historical component-selection proposal. The owner's subsequent direction is
to hand-code a first-person, humanlike companion with OSS as reference code and
tests. See [the active fundamentals](docs/FUNDAMENTALS.md). The product requirements
below remain useful references; OpenClaw ownership is no longer the active design.

Research date: October 13, 2026  
Status: proposed engineering specification; source review completed, integration and hardware tests not yet run.

**Decision**

Build a small distribution of existing projects: a supported ESP32 device, Pipecat voice software, and OpenClaw as the persistent agent. Keep the product-specific work in setup, companion presentation, and packaging.

Use OpenClaw first because Pipecat already provides an OpenClaw gateway service. Reconsider ZeroClaw or PicoClaw only against a measured footprint or deployment requirement. A small executable is not sufficient reason to maintain another integration.

This first release is a single-owner developer kit. It proves provider choice and continuity before custom hardware or a consumer launch.

**1. Minimum user contract**

| ID | Requirement | Acceptance |
| --- | --- | --- |
| P1 | One owner, one named companion, one active conversation | Browser and device reach the same agent. A second active voice session is rejected or explicitly takes over. |
| P2 | Press to talk; explicit stop control | Capture begins only when requested. Stop immediately silences playback and requests cancellation. |
| P3 | Small visible character | Six states: idle, listening, working, speaking, offline, error. State comes from real system events. |
| P4 | Three useful tasks | Remember/retrieve an explicit note; create/list/cancel a reminder; answer a question using web search. |
| P5 | Provider choice | Two independently operated hosted LLM providers and one user-hosted model endpoint work through configuration, without firmware changes. |
| P6 | Independent speech settings | Speech recognition and speech synthesis can be selected independently of the reasoning model. Validate an alternative speech profile. |
| P7 | Durable identity and memory | Name, instructions, saved notes, and reminders survive a host restart and model-provider switch. |
| P8 | User control | Inspect and remove saved notes; inspect/cancel reminders; revoke a device; stop a task. |
| P9 | Self-hosting | A clean install works on a user-controlled Linux host with documented setup and no mandatory account with our project. External service accounts remain necessary for chosen paid APIs. |
| P10 | Recovery | Reconnect does not silently repeat an uncertain write. Recover the operation's status or report that it is unknown. |
| P11 | Backup | A verified backup restores the companion to a fresh instance of the pinned runtime. A shareable template contains no personal state or credentials. |

“Provider independent” means supported interchangeable services, not identical behavior from every model. A model must pass the same tool-use and memory tests before it becomes a supported profile. A configurable endpoint alone is not a compatibility guarantee.

**2. Selected components and ownership**

| Component | Default | What it owns | What we add |
| --- | --- | --- | --- |
| Reference device | M5Stack CoreS3 | Screen, audio, Wi-Fi, USB, board support | Companion display states and controls |
| Device firmware | Pipecat ESP32 client, MIT | Existing media/client implementation | Configuration and authenticated pairing integration |
| Voice service | Pipecat, BSD-2-Clause | Audio pipeline and speech-provider adapters | A small presentation layer around agent events |
| Device/browser transport | Pipecat SmallWebRTC | Direct audio/data transport | Authenticated signaling and session admission |
| Persistent agent | OpenClaw, MIT | Reasoning, provider selection, tools, sessions, memory, scheduling | Restricted companion configuration |
| Voice-to-agent connection | Pipecat OpenClawGatewayService | Existing gateway integration | Convert speech turns to run requests and results to speakable output |
| Memory | OpenClaw native memory and state stores | Durable notes and agent history | Setup defaults and deletion/recovery checks |
| Reminders | OpenClaw native automations | Scheduling and recorded outcomes | Delivery to connected companion UI |
| Skills | Native OpenClaw/Agent Skills directories | Instructions and supporting resources | Three reviewed starter capabilities |
| Admin interface | OpenClaw Control UI plus existing Pipecat browser client | Settings, chat, diagnostics, voice testing | Only the missing companion controls |
| Packaging | Version-pinned release files and Compose profile | Reproducible installation | Setup guide, configuration, update and backup procedures |

The Pipecat ESP32 repository explicitly documents CoreS3 and ESP32-S3-BOX-3. Its build setup exposes Wi-Fi settings and the SmallWebRTC endpoint; polished end-user onboarding is additional work. [S1]

Pipecat's OpenClaw service supports sending, steering, aborting, and streaming run events. It deliberately leaves spoken-turn presentation to its caller. The existing connector removes protocol implementation work, but does not remove the voice UX layer. [S2]

OpenClaw's source is MIT, with third-party notices; Pipecat's core is BSD-2-Clause. Preserve upstream notices and inventory dependencies and character assets separately. [S3, S4]

**3. Hardware minimum**

Use a complete supported development board before designing a PCB.

| Item | v0.1 reference requirement |
| --- | --- |
| Processor | ESP32-S3 |
| Memory | 8 MB PSRAM and 16 MB flash on the reference board |
| Screen | Small color display; CoreS3 provides 2-inch, 320 × 240 touch |
| Audio | Microphone, speaker, and supported audio codec/amplifier |
| Connectivity | 2.4 GHz Wi-Fi |
| Power | USB-C for development; supported battery base for portable testing |
| Interaction | Touch/press to talk, stop, visible capture state |
| On-device computation | Audio transport, display, controls, connection management |
| Model inference | Chosen remote service or a separate user-owned inference host |

CoreS3's documented hardware includes the specified memory, dual microphones, a speaker, and a 500 mAh battery in its supplied DinBase. The main unit is 54 × 54 × 15.5 mm; the complete base assembly is larger. This is a reference device, not the final keychain enclosure. [S5]

Do not infer battery life from capacity. Measure the complete workload. Set the battery-duration and enclosure requirements after the prototype provides power data.

Use existing ESP-IDF provisioning facilities when adding Wi-Fi setup. Select the API version compatible with the firmware's actual toolchain; do not introduce a second embedded framework just for onboarding. [S6]

**4. Deployment minimum**

Start with two application services on one host: the Pipecat voice application and OpenClaw. Keep their durable state in named persistent volumes and preserve separate service lifecycles.

Planning host: 64-bit Linux, 2 CPU cores, 4 GB RAM, and 16 GB free storage. This is a provisional development allocation, not a measured minimum for the combined stack. It excludes local model weights, browser workers, and local speech inference. Use an existing computer first.

OpenClaw documents Raspberry Pi deployments and recommends 4 GB models for comfortable operation. That is evidence for the gateway alone, not a performance guarantee for the combined voice stack. [S7]

There are two network acceptance stages:

- **Local demonstration:** device/browser and host on the same reachable network.
- **Portable pilot:** device on a phone hotspot reaches a chosen remote hub. Authenticate signaling, use encrypted transport, and validate relay fallback.

SmallWebRTC supplies peer-to-peer media, but still requires signaling. Cross-network use may require STUN/TURN: services that discover a network route or relay media when a direct route fails. Reuse coturn for a self-hosted relay if needed. No conferencing server is required for the one-active-session design. [S8, S9]

A host that is asleep cannot run scheduled work. Make this visible in setup and device status. In v0.1, offline behavior is display/control behavior, not offline language intelligence.

**5. State, identity, and portability**

OpenClaw owns agent history, notes, scheduled jobs, and tool outcomes. Pipecat owns the live media session and transient speech buffers. The device owns its connection settings, device credential, display assets, and bounded transient audio.

Use one stable agent identity. Select conversation identifiers deliberately; a fresh socket must not accidentally create a new companion, while independent threads must not be silently combined.

OpenClaw already distinguishes profile information, curated long-term memory, daily notes, and session history. Reuse those surfaces. Begin with explicit “remember this” behavior; add automatic consolidation only when its operation and API costs are understood. [S10]

Define two different artifacts:

- **Companion template:** persona, selected skill files, device assets, dependency versions, and non-secret settings. It is safe to share.
- **Personal backup:** native runtime state and workspace plus required secrets, protected as private data. It restores the same runtime.

Use OpenClaw's native backup/restore process and stop the gateway for a machine-move snapshot. Rebuilding only a memory file does not restore sessions, credentials, schedules, and plugin state. Moving a complete agent to a different runtime is outside v0.1. [S11]

**6. Agent builder minimum**

The builder initially packages an existing OpenClaw workspace and native configuration. Provide three editable choices: persona, enabled capabilities, and provider profiles.

Use SKILL.md for reusable instruction packages. OpenClaw already supports local skill installation and follows Agent Skills. Do not create another skill language, package registry, or dependency resolver. [S12]

The installer must show requested capabilities before enabling them. A skill's text describes behavior; host tool policy enforces authority.

Initial enabled tools are limited to note operations, scheduled reminders, and web search. Shell execution, unrestricted filesystem access, purchases, outbound messaging, and browser automation are outside the starter profile. Later additions use existing runtime permissions and integrations.

A model switch changes inference configuration, not agent identity or durable state. Keep speech recognition, language reasoning, and speech synthesis separately configurable. Local LLM support does not imply that the selected speech services are local.

**7. The product code we actually need**

1. **Companion presentation:** six display states; touch/press controls; spoken replies; readable errors; source links retained in the browser when impractical on the tiny screen.
2. **Setup integration:** Wi-Fi, chosen hub URL, revocable device credentials, model/speech configuration, and a connection test. Keep provider and gateway credentials on the host; do not embed them in distributed firmware.
3. **Voice behavior:** map final transcripts to the existing agent connector; buffer/format text for speech; handle stop, timeout, and disconnection. Start with press-to-talk so acoustic echo cancellation is not an unproven prerequisite.
4. **Release packaging:** compatible version pins, starter workspace, service configuration, persistent volumes, backup instructions, and a focused acceptance suite.

Reuse upstream code for each item where available. Extend or contribute small fixes upstream when practical. A custom replacement is justified only by a documented requirement that an existing component cannot meet.

OpenClaw's scheduler already persists jobs and supports output delivery. Alerting the physical device is additional presentation/delivery wiring; do not assume a scheduler automatically knows how to wake or notify the device. If disconnected, show the recorded result on reconnect rather than claiming it was delivered on time. [S23]

**8. Important integration findings**

The OpenClaw connector source identifies protocol v4, and current OpenClaw documentation also identifies v4. Pin exact releases and test the actual handshake and supported operations; agreement on a protocol number alone does not prove every method is compatible. [S13, S14]

The reviewed Pipecat client documents that a dropped gateway socket fails the in-flight response stream and reconnects for subsequent requests. A failed stream does not prove the underlying tool action did not happen. Recovery must inspect recorded state before retrying a write. [S13]

The service's stop/cancel lifecycle also stops its active run. Therefore do not promise background work continues when the whole voice pipeline is destroyed. Scheduled work belongs to OpenClaw's persistent automation lifecycle. [S2]

OpenClaw's OpenAI-compatible HTTP surface is an existing alternative integration path, disabled by default. It supplies streaming and stable conversation routing. Evaluate it if the native connector cannot satisfy the pinned release, including its distinct cancellation behavior. Keep operator credentials server-side. [S15]

**9. Acceptance tests**

These are proposed engineering gates, not measured results.

| Gate | Test |
| --- | --- |
| Reproducible install | A fresh host starts from the documented release package; persistence survives recreation of application containers. |
| Voice | Complete 30 ordinary turns; record median and 95th-percentile end-of-speech to first useful spoken answer. Initial target: median ≤2 seconds, p95 ≤4 seconds on a documented reference network, excluding tool-heavy requests. |
| Stop | Local audio stops within 250 ms of explicit stop; backend cancellation is requested and its eventual status shown. Completed external actions are not described as undone. |
| Memory | Save 10 known notes; restart the host; retrieve each. Delete a note and confirm it no longer appears in the documented active recall surfaces. |
| Providers | Run the same note, reminder, and search scenarios with two hosted LLM providers and one compatible local endpoint; preserve identity and saved state. |
| Speech choice | Replace the speech profile and repeat basic conversation without changing firmware or agent identity. |
| Reminders | Create/list/cancel jobs, restart before due time, and check recorded delivery outcomes. Test device offline separately from host offline. |
| Recovery | Inject disconnects before dispatch, during execution, and after completion. No blind replay of uncertain writes; unknown outcomes remain explicit. |
| Two endpoints | Continue from device to browser using the intended conversation; enforce one active voice session. |
| Permissions | Disabled tools remain unavailable even when requested in an instruction or returned by a search result. |
| Backup | Restore to a fresh host using native backup tools; verify identity, notes, schedules, and required configuration. |
| Portability | Run the device through a phone hotspot and relay fallback before calling it a portable pilot. |
| Resources | Record combined idle/active RAM, CPU, network traffic, model/speech cost, and device power. Exclude inference-host usage only if clearly reported separately. |

Include quiet-room, background-conversation, and street-noise samples in the voice evaluation. WearVox specifically evaluates wearable conditions such as noisy audio and side-talk rejection; text-only tests do not cover those failures. [S16]

**10. Alternatives reviewed and when to use them**

| Alternative | Fit | Decision |
| --- | --- | --- |
| Xiaozhi firmware + xinnan-tech server | Existing integrated ESP32 voice path; server offers multiple providers and memory integrations | Good fallback for a voice-device demonstration. The server's own README says it is not ready for production use. Avoid running its full voice stack alongside Pipecat without a specific need. [S17] |
| ZeroClaw | Compact Rust runtime with providers, memory, tools, and gateway | Candidate if measured host footprint or deployment constraints justify replacing OpenClaw. Integration and migration would need separate validation. [S18] |
| PicoClaw | Go runtime for small Linux systems | Evaluate under the same workload if the default host is too large. Do not equate runtime RAM with total speech/inference RAM. [S19] |
| Espressif ESP-Claw | Apache-licensed embedded agent runtime, event handling, MCP, and provider endpoints | Strong alternative if keeping the agent loop on the microcontroller becomes a firm requirement. Reviewed material does not establish the complete voice-companion flow proposed here. [S20] |
| MimiClaw | Embedded memory, tools, scheduling, and provider calls | Relevant smaller architecture, but the reviewed main README does not establish an integrated voice path. [S21] |
| XiaoClaw | Combines Xiaozhi voice with MimiClaw-style agent behavior | Closest embedded combination to inspect. Its README uses “local inference” language while showing remote LLM APIs; do not interpret that as proven offline frontier inference. Validate its build, flash layout, and required speech backend before adopting it. [S22] |

**11. Build order and decision rules**

- **First:** browser → Pipecat → existing OpenClaw connector. Demonstrate memory, one reminder, provider switching, and stop. Freeze the working versions.
- **Next:** flash the supported CoreS3 client. Add the six display states and controls.
- **Then:** package setup, permissions, native backup, and reconnection handling.
- **Finally:** measure battery and cross-network use before selecting a smaller enclosure or designing a custom board.

Each step has a concrete exit condition. Stop adding components once the acceptance tests pass. If the integration fails, evaluate the existing HTTP route or an upstream fix before changing runtime.

Deferred: custom PCB, continuous environmental recording, camera behavior, autonomous browsing, multi-agent teams, app marketplace, multi-tenant cloud hosting, native mobile hub, and a universal cross-runtime agent format.

The first reusable product is a tested configuration and device experience built on existing systems. Its value is how little a builder must assemble and maintain.
 
**Sources**

All sources below were reviewed on October 13, 2026. Repository and documentation claims are not independent performance verification.

- [S1 — Pipecat ESP32 client](https://github.com/pipecat-ai/pipecat-esp32)
- [S2 — Pipecat OpenClaw gateway service](https://reference-server.pipecat.ai/en/latest/api/pipecat.services.openclaw.gateway.html)
- [S3 — OpenClaw repository and license](https://github.com/openclaw/openclaw)
- [S4 — Pipecat license](https://github.com/pipecat-ai/pipecat/blob/main/LICENSE)
- [S5 — CoreS3 hardware documentation](https://docs.m5stack.com/en/core/CoreS3)
- [S6 — ESP-IDF Wi-Fi provisioning](https://docs.espressif.com/projects/esp-idf/en/v5.5.3/esp32s3/api-reference/provisioning/wifi_provisioning.html)
- [S7 — OpenClaw Raspberry Pi deployment](https://docs.openclaw.ai/install/raspberry-pi)
- [S8 — Pipecat SmallWebRTC](https://docs.pipecat.ai/api-reference/server/services/transport/small-webrtc)
- [S9 — coturn](https://github.com/coturn/coturn)
- [S10 — OpenClaw memory](https://docs.openclaw.ai/concepts/memory)
- [S11 — OpenClaw migration and backup](https://docs.openclaw.ai/install/migrating)
- [S12 — OpenClaw skills](https://docs.openclaw.ai/tools/skills)
- [S13 — Pipecat OpenClaw client source](https://reference-server.pipecat.ai/en/stable/_modules/pipecat/services/openclaw/client.html)
- [S14 — OpenClaw protocol versioning](https://docs.openclaw.ai/gateway/protocol/versioning)
- [S15 — OpenClaw compatible HTTP endpoint](https://docs.openclaw.ai/gateway/openai-http-api)
- [S16 — WearVox](https://arxiv.org/abs/2601.02391)
- [S17 — Xiaozhi ESP32 server](https://github.com/xinnan-tech/xiaozhi-esp32-server)
- [S18 — ZeroClaw](https://github.com/zeroclaw-labs/zeroclaw)
- [S19 — PicoClaw](https://github.com/sipeed/picoclaw)
- [S20 — Espressif ESP-Claw](https://github.com/espressif/esp-claw)
- [S21 — MimiClaw](https://github.com/memovai/mimiclaw)
- [S22 — XiaoClaw](https://github.com/beancookie/xiaoclaw)
- [S23 — OpenClaw automations](https://docs.openclaw.ai/automation/cron-jobs)

