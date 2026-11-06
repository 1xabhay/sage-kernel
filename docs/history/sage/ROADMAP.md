# Roadmap: Sage kernel, with Reachy Mini as the first body

Two goals at once: Sage grows into a device-agnostic kernel for always-on
agents, and an always-on Reachy Mini pet is its first showcase. Every slice
follows the house TDD order: contract → failing behavioural test → code →
check on the real boundary (simulator or robot) → commit.

## Where we are (2026-10-20)

Running live: the pet is a light client of the kernel. It hears, speaks and
moves; identity, memory and decisions live in the kernel (Docker on the Mac).

- Device protocol v0 (`DEVICE-PROTOCOL.md`): hello, events, long-polled effects,
  tokens per device, journal sequence as cursor. Black-box suite:
  `tests/test_conformance.py` (`make conformance`).
- Emote travels with the reply; the device declares its vocabulary.
- Busy → retry same turn ID ~5 s → "one moment". Fresh-or-silent replies after
  a reconnect. A device clears its own interrupted turn.
- Sealed containers (`tests/test_deploy.py`), Colima VM mounts narrowed, robot
  SSH key-only in practice (random password in Keychain), robot API locked to
  this Mac by a runtime-only nftables rule.

The design rule for everything below: **one loop, small vocabularies.** A sense
becomes an event, the kernel decides (saying nothing is a valid decision), and
effects are authorized against the body's manifest. A new ability is one new
sense, one new effect, or one new rule, never new plumbing.

## Owner actions (no code)

1. Router: reserve fixed LAN addresses for the Mac and the robot
   (its hardware address is in the router). Then ask to persist the robot firewall rule.
2. Review and merge `sage-light-client` in reachy-mini (pet-side client, echo fix).
3. Decide: regenerate the gateway `requirements.lock` (8 pillow advisories,
   fixed in 13.0.0) or drop the gateway experiment so `make vuln` passes.

## Principles added 2026-10-20

- **Everything is a plugin.** A robot, a browser, a phone and the agent's own
  computer (shell, files, browser, web, MCP servers in a sandbox container) all
  speak the same device protocol. **The kernel never gets its own shell**: it
  stays a small, auditable decision-maker, and all its power comes from plugins
  it authorizes.
- **The action space is a tool list.** Manifests take the shape of an MCP tool
  list (`name`, `description`, `inputSchema`, plus limits such as `max_s` and
  `interruptible`). The list is the same on every turn; a verb a body lacks
  returns a clear error instead of disappearing. Every effect reports a result.
- **Risk tiers are the kernel's, not the model's:**

  | Tier | Examples | Rule |
  | --- | --- | --- |
  | Read | search, fetch, read file | automatic |
  | Body | speak, emote, look_at | automatic, bounded, interruptible |
  | Change | write files, run commands, install | sandbox only, kernel review |
  | Outside world | send messages, purchases, posts, the host | owner approval; no answer means no |

- **Source is tracked.** Speech, web pages and command output enter as
  untrusted information, never as instructions.

## Next slices, in order

| # | Slice | First failing tests | Done when |
| --- | --- | --- | --- |
| 1 | **Conversation quality + interface gaps**: repetition guard, prompt trace, Stop delivered to the device that is acting, a result event per effect, per-device event rate limit | Stop in the browser silences the robot within one step; a reply equal to one of the last 3 is rejected with a reason; each decision journals its prompt; a flood of events is refused with a code | Stop works across bodies; any reply traces to its prompt |
| 2 | **Native tool calling + MCP-shaped manifests** | a manifest tool becomes a JSON-schema tool for the model; a tool call outside its schema is rejected; the tool list is identical across turns | emote is a real tool call, not a parsed label |
| 3 | **Simulator layer**: `reachy_mini[mujoco]`, the real pet against `reachy-mini-daemon --sim`, a desk scene | "look left" → head yaw within limits → target moves in the rendered frame | `make sim` passes; a limits report |
| 4 | **Motion verbs** as tools: `look_at`, `turn_body` | an undeclared verb rejects the decision; Stop cancels a motion within one step | "look at me", "turn left", "nod" on the robot |
| 5 | **Queue, background jobs, per-device turns** | a long job never blocks the pet; its result arrives later as an event; an interrupted turn blocks only its device | the pet keeps talking while work runs |
| 6 | **Agent-computer plugin**: sandbox container (workspace mount only, egress allow-list, limits, secrets as env), risk tiers, approval in the browser, untrusted labels | an outside-world effect waits for approval and times out to "no"; command output cannot issue tools; the kernel container still has no tools | "look this up and tell me" and a small sandboxed coding task |
| 7 | **Disposition and presence**: quiet / available / engaged / restless; presence and timer events | "quiet" holds until spoken to; nudges within budget; greet once per arrival | it behaves like a pet |
| 8 | **Observation**: local vision model, a screens rule | image bytes never leave the Mac or reach the journal | "what's on my desk?" is grounded |
| 9 | **Memory tiers**: profile, dated log, search | facts outlive 10 exchanges; memories are information, not instructions | it remembers you across days |
| 10 | **Freeze protocol v1**; the emote blend; the browser on `/v0`; then the Rust gate | the conformance suite with several form factors | the kernel is ready for others to build on |

## Deliberate shortcuts to revisit (`ponytail:` notes in code)

- Manifests and device emotions are held in memory; devices re-send hello.
- `Store.seen` scans the journal; add an expression index when it shows up.
- An undeclared emote is dropped, not blended (owner wants blend: slice 7).
- The pet keeps `Mind`/`local_mind` only for `reachy-pet-bench`; move the bench
  into sage once the kernel prompt settles.
- Barge-in is off until `say()` can be interrupted; there is no wake word or
  "was that meant for me?" check yet (fold into slice 4).

## Test layers

1. Pure policy (ms): conductor, disposition, authorization, property tests.
2. Kernel conformance (s): HTTP only, any kernel implementation.
3. Simulator (tens of s): the real pet against the MuJoCo daemon; the kernel
   cannot tell a simulated body from a real one.
4. Hardware: the scripted demo for acoustics, lag and feel.
