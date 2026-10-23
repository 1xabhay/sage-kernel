# Sage: a first-person companion we hand-code

The [Sage Kernel charter](KERNEL-CHARTER.md) is the active direction. This document
describes the existing browser prototype; its earlier product gates below are
historical. The selected-components decision in the old minimum spec is also historical.
OSS is a source of reference implementations and test cases; it does not define
the product or own Sage's identity and state.

## The minimum loop

```text
what you share → observation → current context → proposed reply
              → permitted output → actual feedback → updated context
```

“First person” here means a stable named character interacting from its own
limited viewpoint. Its body and words express the same actual state. It can
respond to what you shared and retain context. This does not require a new OS,
multiple agents, a task dashboard, or a general tool framework.

| Fundamental | Smallest implementation | Evidence |
| --- | --- | --- |
| Identity | Name and first-person instructions, ordinary immutable values | Same identity across turns/restart |
| Observation | Text deliberately submitted by the owner | No implied camera/microphone access |
| Context | Last ten completed exchanges, with user/companion roles | Ordering and truncation tests |
| Decision | Injected provider returns a `Decision` containing proposed effects | Compatible model adapter tested against a local HTTP fixture |
| Authority | Exactly one bounded text reply to the requesting endpoint | Motion and replies to other endpoints are rejected and journaled |
| Expression | A small original character, reply bubble, optional browser speech | Working state from dispatch; speaking from actual playback |
| Continuity | Private SQLite journal and state checkpoint committed together | Process-death, outcome, migration, rollback, and backup checks |

The model context is deliberately small. The durable journal retains all admitted
events and observed outcomes, including exchanges outside that context window.
Explicit notes, retrieval, reminders, additional senses, and automatic memory
consolidation remain separate work.

## Understanding one turn

State is a value: identity, completed exchanges, and the status of the current
turn. An event is something observed, such as submitted text or playback ending.
An effect is a requested action, such as calling a model. Requested effects do
not establish success; the shell must report completion or failure.

For example, submitting “hello” calculates a new working state and a model-call
effect. The shell supplies the model with identity, completed context, and
“hello”. A valid reply becomes a completed exchange only after its private
transaction commits. The body starts speaking only when the browser reports actual
playback. Stop suppresses future output from that turn. Its eventual reply is
excluded from model context. Its completion is still recorded in the private journal.

Pure means the same state and event produce the same result without changing
the original state. This makes policy tests fast and reproducible even when
model replies vary. Python's frozen dataclasses and tuples express this directly;
there is no functional framework. The network, filesystem, IDs, and threads
belong to the shell. Modules separate responsibilities; they need not become
separate services.

## Functional boundary

```text
advance(companion, event) → (new companion, requested effects)
context(companion)        → model messages
```

`sage/turn.py` remains the pure turn policy, including the earlier single-endpoint
capture/playback contract. `sage/companion.py` adds identity, bounded dialogue,
and the current turn's source. Neither performs I/O.
`sage/storage.py` commits the journal and checkpoint in one SQLite transaction.
`sage/kernel.py` coordinates shared state, the injected decision provider, and
reply authorization. It commits each event and its requested effects before
dispatching the provider. `sage/model.py` adapts the chat endpoint to `Decision`.
`sage/web.py` adapts HTTP and browser playback to the same kernel.

Kernel connection and playback events are observations, not global turn-state
changes. A device disconnect does not disconnect Sage, and one device's playback
does not block another device's input. The browser adapter still prevents input
while its own reply is playing. Only final text enters the decision path; raw
audio/video and the earlier capture events remain outside the kernel.

The shell constructs `Kernel(Store(database), decide)`, then `Runtime(kernel)`
for the browser. A second adapter sends `Event("submitted", turn_id, text,
source=device_id)` to that same kernel. The mock body uses this interface without
kernel changes. `tests/test_kernel.py` supplies a mock text body and a deterministic
decision provider to exercise restart, recall, and disconnects.

Tests use behavioral examples: a submitted message never starts a microphone;
stop suppresses a late reply; an old callback cannot animate a new turn;
only completed exchanges enter context; a provider/config failure stays visible.
No test claims that the model's exact wording is deterministic.

The journal records event sources, effect destinations, decisions, and rejected
proposals. Existing checkpoints and journal records lacking the new fields load
with their original data and neutral defaults; no database rewrite is required.

## First local slice

```sh
make up
make check
```

Open `http://127.0.0.1:8765/`. The character is the main interface. Type to it,
optionally enable spoken replies, and use Stop. Browser speech is an output
adapter; microphone capture is not implemented in this slice.

Without model configuration, the page clearly says **Preview · authored replies**.
That mode exercises the interaction and state transitions; it is not an AI
conversation. For a real compatible model endpoint, put these in a private `.env`:

```dotenv
SAGE_MODEL_URL=http://host.docker.internal:11434/v1
SAGE_MODEL=your-installed-model
SAGE_API_KEY=
```

This example assumes an inference server you already operate; it downloads no
model. Hosted endpoints require HTTPS; credentials remain on the server.
Run `make up` again after changing configuration. An existing database preserves
the companion name and completed context when inference configuration changes.
The laptop's private `.env` now selects the already-installed `qwen2.5:7b` through
Ollama's [compatible endpoint](https://github.com/ollama/ollama/blob/main/docs/api/openai-compatibility.mdx).
The adapter requests at most 256 output tokens and waits at most 30 seconds.
Use one active browser tab for this prototype.

Stop silences browser playback immediately and suppresses a late reply. The
non-streaming model adapter may finish remotely; no claim of remote cancellation
or rollback is made. Another turn waits until that request settles. Interrupted
in-flight replies become unknown after restart, retaining the input and turn ID.
The browser offers **Continue** to acknowledge that uncertainty before starting
another turn. This does not retry or claim cancellation of the interrupted reply.
Completed and failed turn outcomes remain known, and old audio is suppressed.

On first startup with no database, `/data/companion.json` is imported into
`/data/companion.sqlite3` without modifying the JSON file. Later startups use the
database. A damaged legacy snapshot or database stops startup rather than resetting
memory. Migration cannot recover exchanges the old snapshot already discarded.

## References and earlier work

We reuse ideas and test cases from the original Pipecat/OpenClaw source review:
correlated runs, actual audio lifecycle, stop/completion races, and explicit
uncertainty. The model adapter follows the compatible chat endpoint documented
by [llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
The local shell uses Python's [HTTP server](https://docs.python.org/3.12/library/http.server.html)
and browser [speech synthesis](https://developer.mozilla.org/en-US/docs/Web/API/SpeechSynthesis).
These remain a private localhost prototype, not public Internet hosting.

The earlier gateway package is retained as `compose.reference.yaml` and
`Dockerfile.reference`. Its volumes and backups remain intact. The native
gateway is reference/test infrastructure rather than the Sage product entry.
Use `make reference-up` or `make reference-check` when deliberately checking
that integration; do not confuse its 24 earlier tests with companion acceptance.

Next sequence: test the real-model character interaction with the owner → add
a permissioned microphone/STT adapter → add
explicit notes → reminders/search with validated capabilities → hardware/cloud.
Independent work can compare model/speech adapters against the frozen event
contract; identity, state transitions, and permissions remain one shared core.

## Earlier course and gates (historical)

Each slice follows: write the observable contract → write a failing behavioral
test → implement → check the real boundary → record evidence. Change the
contract before changing the implementation when the intended behavior changes.

| Gate | Dependency | Exit evidence | Current state |
| --- | --- | --- | --- |
| F0: fundamentals | Owner's product correction | Explain identity, observation, context, policy, effects, feedback | Written here |
| F1: typed companion | F0 | First-person character; real reply; correlated Stop; bounded context survives restart | Implemented; automated/live checks pass; owner experience pending |
| F2: voice input and conversation | Owner approves F1 interaction | Explicit mic permission; transcripts become the same submitted event; actual speech lifecycle; measured Stop | Pending |
| F3: explicit memory | Stable F1/F2 context contract | Save/read/delete a named note; permission and restart checks | Pending |
| F4: useful actions | F3 capability contract | One reminder and sourced search; validate results independently of prose | Pending |
| F5: portable execution | Working local conversation | Fresh private Linux install, backup restoration, chosen hardware acceptance | Source package ready; execution pending |

```text
F0 → F1 → owner interaction check → F2 → F3 → F4 → F5
      └→ packaging / private Linux preparation ─────────→ F5
```

After F1 freezes the event contract, browser appearance, speech-adapter research,
and packaging can proceed independently with isolated state. Mic capture and
playback integration must join before the F2 gate. Changes to identity, admission,
and shared context remain sequential. Within the running app, inference happens
outside the state lock; events and snapshots are serialized, with one active
model request per companion.

Feedback targets: pure core checks under one second; local adapter checks under
ten seconds, excluding image build; live model checks separately. Measure useful
reply latency and actual local audio silence before optimizing. The first two
model turns are a smoke test, not the original spec's 30-turn voice baseline.
Owner testing now focuses on whether this interaction feels like the intended
pet. The next implementation gate depends on that experience, rather than adding
tools or a second runtime before the product loop is right.

## Reuse locally and later on a private VM

`make up`, `make check`, `make status`, `make logs`, and `make down` operate this
companion. `make backup` uses SQLite's online backup API to create a consistent
snapshot in a fresh private directory under `.backups/`; `make wheel` builds the
dependency-free Python package;
`make package` creates `dist/sage-companion-0.2.0.tar.gz`. The Docker build and
deployment archive exclude `.env`, generated state, and private backups.

For a future owner-controlled Linux VM, extract the deployment kit into a fresh
directory, set its private `.env` to a model reachable from that VM, then run
`make up` and `make check`. `host.docker.internal` in the laptop example is a
Docker Desktop host address; it does not transfer the laptop's Ollama server to
Linux. From the laptop use `make cloud-tunnel CLOUD_HOST=user@host`, then the
same localhost:8765 URL. The local service must release that port first. Keep
the VM port bound to loopback. Public hosting/authentication and actual cloud
execution are separate pending gates.

The source kit carries code, not companion continuity. A database snapshot contains
private conversation text. To move the companion, stop admission and back it up,
then install the snapshot as `/data/companion.sqlite3` with mode 0600 and UID 1000
in a fresh private data volume. Maintain one active state owner; stale writers
are rejected rather than overwriting newer state. The tests restore SQLite
backups; fresh-volume deployment is still an acceptance check to run.
