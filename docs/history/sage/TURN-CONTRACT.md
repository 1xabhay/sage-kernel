# First executable contract: one local voice turn

Scope: deterministic policy for one admitted endpoint. Requirement mapping:
parts of P2 (controls), P3 (display), and P10 (no blind reconnect replay).
P1 authentication/admission, real media, upstream cancellation, persistent
receipts, and all durable agent behavior are later integration contracts.

The hand-coded companion also uses `submitted(turn_id, text)` for typed input.
It obeys the same turn admission rules and requests `send_turn` directly,
without starting or stopping capture. See [FUNDAMENTALS.md](FUNDAMENTALS.md).

## Values

`State` is immutable: connected, capturing, awaiting a final transcript, playing, local turn ID, run status,
and whether stop/disconnect suppressed further audio for this turn.
Run statuses: none, active, completed, cancelled, failed, unknown.
“Active” begins when dispatch is requested; failure to confirm dispatch may be
unknown. It is not proof the gateway accepted the request.

`Event` carries a kind, optional local turn ID, and optional transcript text.
The shell creates fresh turn IDs outside the core and correlates upstream run
IDs to them. Network input is authenticated and decoded at the shell boundary;
the reducer additionally rejects unknown event kinds, missing required IDs,
and blank transcripts.

`Effect` is an immutable request: start/stop capture, send turn, silence playback,
or abort run. It carries the local turn ID and text where needed. Effects are
returned in execution order; the reducer performs none of them itself.

## Rules and examples

| ID | Trigger | Required behavior |
| --- | --- | --- |
| C1 | Initial value; connected/disconnected | Offline initially; idle on first connect; actual transport status wins display priority |
| C2 | Press with a fresh ID while connected and free | Start capture, become listening; reject overlapping capture/run/playback or an unresolved unknown turn |
| C3 | Release, then final nonblank transcript for captured turn | Release stops capture immediately and becomes working while awaiting STT; transcript requests one dispatch; if transcript precedes release, stop capture before send |
| C4 | Duplicate or stale correlated callback | Return unchanged state, no effects; a second transcript never dispatches twice |
| C5 | Real playback starts/ends | Speaking only on actual playback start; when it ends return working if run still active, otherwise idle/error |
| C6 | Run completes before queued audio finishes | Record completed while keeping speaking until playback drains |
| C7 | Explicit stop | Silence playback and stop capture first; request abort only for active work; prevent later audio from resurrecting stopped turn |
| C8 | Stop racing with completion | Preserve completed; no claim of undo; cancellation remains a separate remote result |
| C9 | Disconnect while dispatch/run uncertain | Silence and stop capture, mark active run unknown; reconnect never sends or aborts automatically; display error after reconnect |
| C10 | Exact terminal evidence for current unknown run | Resolve unknown from trusted correlated evidence; stop/failed/cancelled suppress further playback; completed cannot be overwritten by a contradictory late terminal callback |
| C11 | Known run failure | Record failed and silence; error is visible when connected; failed means the run ended, not that no tool side effect happened |
| C12 | Invalid input | Raise ValueError for unknown kind, missing required ID, or blank final transcript; shell must show a readable error and avoid dispatch |

Normal example:

```text
offline → connected → idle
press(t1) → listening + start_capture(t1)
released(t1) → working + stop_capture(t1)
transcript(t1, "hello") → working + send_turn(t1, "hello")
playback_started(t1) → speaking
completed(t1) → speaking (run completed, audio still playing)
playback_finished(t1) → idle
```

Uncertain-write example:

```text
transcript(t1, "remember ...") → dispatch requested
disconnected → offline, run unknown, local audio suppressed
connected → error, no dispatch; press(t2) is refused pending reconciliation
completed(t1) from exact trusted evidence → idle, no automatic speech/replay
```

Display priority: offline → unknown/failed error → listening → speaking →
awaiting-transcript/active working → idle. An active run with explicit stop displays idle while
remote cancellation is pending; the browser must also display run status.
Unknown blocks another turn in this first policy; M4 needs a documented owner
acknowledgment/reconciliation path if exact evidence cannot be recovered.

## Shell obligations

Serialize events. Preserve IDs across callbacks. Run capture-stop and local
silence without waiting for the gateway. Prevent queued/future audio for a
suppressed turn. Real effect failures must produce failure/disconnect events;
the reducer returning a new value is never an I/O success assertion.
Normalize a connector's transport-related stream failure to `disconnected`,
not `failed`: only known terminal failure evidence uses the latter. A stream
ending with an error cannot establish that an underlying write did not happen.

The browser adapter must report physical release immediately and wait separately
for final STT. It must not leave recording active until STT answers. No real
microphone is controlled by the current core. A cancelled pre-dispatch turn
ignores a late transcript.

Cancellation is not rollback; failed runs can have completed side effects.
Persist correlation/receipts before dispatch at M4 if native recovery records
cannot meet crash recovery. This in-memory reducer cannot satisfy host-restart
recovery, per-device revocation, or cross-process session admission by itself.

The current shell now commits events, effects, and state atomically in SQLite
before model dispatch. `restarted` clears capture/playback and suppresses old
output, preserving known outcomes and marking an active run unknown. The owner
can acknowledge an unknown run with `acknowledged` to admit a new turn without retrying the old
one. The journal retains the uncertainty and the acknowledgement. This provides
text-turn restart recovery; device authority and physical effects remain pending.

The shared kernel now treats `connected`, `disconnected`, and playback events as
observations. It journals their endpoint source without applying the older
single-endpoint connection/playback transitions to shared agent state. Browser
connection and playback state belong to its adapter. Provider decisions propose
effects; the current authorization policy admits one text reply to the requesting
source. Physical effects are rejected. The reducer tests above remain the
single-endpoint policy contract; `tests/test_kernel.py` proves shared behavior.

Read `tests/test_turn.py` as examples and run:

```sh
python3 -m unittest discover -s tests -v
```
