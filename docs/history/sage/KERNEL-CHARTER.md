# Sage Kernel — Engineering Charter v0.1

**Purpose:** Build a minimal, self-hosted, model-independent runtime for persistent personal agents.

**First experiment:** Give Sage a physical body using Reachy Mini, while retaining web-browser interaction with the same agent.

## Core architecture

```text
Browser ─────┐
             ├── Sage Kernel ── State & History
Reachy Mini ─┘        │
                      ├── Decision Provider
                      └── Authorized Effects
```

Sage consists of four fundamental abstractions:

1. **Event** — Something happened.
2. **State** — What Sage knows and remembers.
3. **Decision** — What should happen next.
4. **Effect** — An operation Sage proposes or performs.

The kernel coordinates these abstractions, records execution, and enforces authorization.

## Architectural rules

- Sage's identity and memory are independent of its devices.
- The kernel must not depend on the Reachy SDK, any LLM vendor, or frontend technology.
- Browser and Reachy interactions share the same state.
- The system activates through events, not constant model inference.
- Streaming audio/video is processed separately from durable agent events.
- Physical actions require bounded, interruptible execution.
- Processes can restart without losing recorded history or known execution outcomes.

## First implementation

Use Python, FastAPI, asyncio, SQLite, the official Reachy Mini SDK, and a replaceable model API.

No agent framework, vector database, distributed scheduler, or model training is required.

## First proof

Tell Sage something through the browser, restart Sage, then ask Reachy to recall it.

Next, test one visual observation, one authorized motion, and one proactive greeting.

Finally, disconnect Reachy. Sage must continue operating through the browser.

**Success criterion:** Replacing Reachy with a mock device requires zero changes to the kernel.

## Implementation status

The first TDD slice provides a transactional SQLite journal and state checkpoint.
It records admitted events, requested effects, and observed turn outcomes; model
context remains the last ten completed exchanges. Legacy JSON snapshots are
imported once and retained unchanged. Restarted active turns become unknown,
require an explicit acknowledgement, and are never replayed.

The second TDD slice extracts `sage/kernel.py`: one coordinator accepts events
from endpoints and calls an injected `Companion → Decision` function. Decisions
contain proposed effects; the kernel currently authorizes exactly one bounded
text reply addressed to the requesting endpoint. Decisions and rejections enter
the journal before the correlated completion or failure. The compatible chat
provider lives in `sage/model.py`; browser rendering and playback live in
`sage/web.py`. The kernel imports neither adapter.

Event sources and reply destinations are explicit. Connection and playback
observations are journaled without changing global agent availability or invoking
the provider. Each adapter owns its local connection/playback state. The HTTP
adapter assigns its own source and rejects client-supplied sources.

Tests prove browser → restart → mock-device recall, lifecycle events without
inference, and continued browser use after mock disconnect. The deterministic
provider fixture checks the shared context; it does not establish live-model
recall quality, acoustic playback, or hardware behavior.

The third slice adds device protocol v0 ([DEVICE-PROTOCOL.md](DEVICE-PROTOCOL.md)):
a token-authenticated listener beside the browser's, with hello, events and a
long-polled effects cursor taken from the journal's own sequence. A turn ID is
admitted once per source, a decision and its outcome commit in one transaction,
only completed and unstopped replies are delivered, and only the device that
started a turn may acknowledge it. `tests/test_conformance.py` checks all of it
over HTTP against a kernel subprocess, so another implementation can be held
to the same suite. The stdlib server stands in for FastAPI/asyncio until a
measured need appears.

Authorized motion, visual observations, and proactive events remain to build. The Reachy application in the adjacent workspace still
runs independently. Physical effects remain denied until a bounded, interruptible
executor and its authorization tests exist.
