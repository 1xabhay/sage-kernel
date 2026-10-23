# Decision register

Status: **Owner** = decided by the owner. **Lead** = chosen during the work and
awaiting the owner's confirmation. **Open** = not decided. Update a row rather
than adding a new one; record the date when its status changes.

## Models

| Area | Current choice | Where set | Status | Notes |
| --- | --- | --- | --- | --- |
| Conversation LLM | `qwen2.5:7b` via Ollama, OpenAI-compatible endpoint | the previous kernel's `.env`, `SAGE_MODEL` | **Open** | The pet's bench picked `qwen3:1.7b` (3× faster, 8/8 valid emotions), but the kernel's endpoint can't send `think:false`, so qwen3 would think away its tokens. Options: keep 7b, add native Ollama `/api/chat` support to the kernel adapter, or try a larger model |
| Speech to text | faster-whisper `base.en`, CPU int8, on the pet | `REACHY_PET_WHISPER` | **Open** | Mishears ("F-mer commutator"). `small.en` is more accurate and slower; measure first |
| Voice detection | Silero v5 ONNX, on the pet | fixed | Lead | |
| Text to speech | piper `en_GB-jenny_dioco-medium`, on the pet | `REACHY_PET_VOICE` | Lead | Voice choice is part of the character |
| Face finding | the robot daemon's own tracker + body-yaw follower | fixed | Lead | |
| Vision (descriptions) | none yet; proposed: local vision model via Ollama | — | **Open** | Slice 5. Must stay on the Mac |
| Where models run | only on the Mac; no cloud LLM, STT or TTS | — | Owner | |

## Configuration

| Setting | Value | Status |
| --- | --- | --- |
| Kernel ports | browser 127.0.0.1:8765, devices 127.0.0.1:8766 | Lead |
| Device tokens | `SAGE_DEVICES=name=token` in `.env` (mode 600); issuing a token is consent | Owner |
| Reply freshness | 10 s by the kernel's clock (`SAGE_FRESH_SECONDS`) | Owner |
| Busy retry | 5 s, same turn ID, then "One moment." | Owner |
| Reply wait on the pet | 30 s | Lead |
| Offline line | "I can't reach my memory right now.", at most once a minute | Lead |
| Model call | non-streaming, ≤ 256 output tokens, 30 s timeout | Lead (inherited) |
| Context window | last 5 to 8 exchanges, cut back in blocks so the prompt cache holds | Lead (inherited); slice 6 adds memory tiers |
| Mic deaf after speaking | 1.0 s (`REACHY_PET_TAIL_GATE`); barge-in off | Lead |
| Robot address | a fixed LAN IP in `REACHY_MINI_HOST` | Lead; router reservation is an owner action |

## Architecture

| Decision | Status |
| --- | --- |
| Light client, heavy server: the pet senses and acts; identity, memory and decisions live in the kernel | Owner |
| One loop, small vocabularies: sense → event → decide (silence allowed) → authorized effects | Lead |
| Docker for the local environment + a Makefile for every task (`up`, `check`, `conformance`, `lint`, `imports`, `vuln`, `hygiene`) | Owner |
| Kernel on the Mac in Docker (Colima); the pet on the Mac too | Owner |
| Python kernel is the reference; Rust only after v1 has been frozen and stable for two slices and there is a real need, and it must pass the HTTP conformance suite | Owner |
| Stdlib HTTP server instead of the charter's FastAPI/asyncio, until a measured need | Lead (differs from the charter) |
| Long-polling over plain HTTP + JSON instead of WebSockets or SSE | Lead |
| No outbox table: deliveries are derived from the journal; the journal sequence is the cursor | Lead |
| Protocol versioned in the path (`/v0`), freeze v1 after slice 3's emote + presence have run through it | Lead |
| A device's manifest is trusted as declared (no grants file) | Owner |
| An interrupted turn is acknowledged only by the device that started it; it should block only that device (per-device turn state is slice 7) | Owner |
| A reply missed while away is spoken only if fresh, otherwise only remembered | Owner |
| Feelings: each device declares its own names; the kernel offers exactly those; one emote per reply, played just before speech | Lead |
| An unsupported feeling: blend the two nearest declared ones (valence/arousal) | Owner (interpretation unconfirmed); dropped until slice 7 |
| No local fallback mind when the kernel is unreachable (two minds would be two identities) | Lead |
| The pet's old `history.json` is left on disk, unread, not imported | Lead |
| Everything is a plugin, including the agent's computer; the kernel never gets its own shell | Owner (2026-10-20) |
| Manifests shaped like an MCP tool list; the same tool list every turn; unavailable verbs return a clear error; every effect reports a result | Lead (agreed in principle 2026-10-20) |
| Effect risk tiers: read automatic; body bounded; change sandbox-only with kernel review; outside world needs owner approval, no answer means no | Lead (agreed in principle 2026-10-20) |
| Untrusted sources (speech, web, command output) enter as information, never instructions | Lead (agreed in principle 2026-10-20) |
| Long work runs as background jobs whose results arrive as events; the pet never waits on them | Lead (agreed in principle 2026-10-20) |
| Persona: the kernel's existing system prompt, unchanged so its start stays stable for caching; device notes go on the newest message | Lead. **Conflict:** that prompt says "do not claim camera, microphone", which is now untrue for the robot |
| Protocol: an optional manifest `body` (≤ 300 characters), shown beside the senses in the cached prefix, so a body can name parts it cannot use yet ("a camera, but I can't see through it yet"); it declares no sense (PROTOCOL §2) | Owner (2026-10-22) |
| Protocol: a `done` result's `text` reaches the model on the next turn as the body's report after that exchange, first 120 characters (PROTOCOL §5) | Owner (2026-10-22) |
| Protocol: notes ride only on turns they match (a passage after the card scores above 0, or the line names the topic); an unrelated line gets none (PROTOCOL §10) | Owner (2026-10-22) |

## Open questions for the owner

1. Which conversation model, given the latency and quality trade-off above?
2. Rewrite the persona prompt for a body that hears and sees, once, deliberately?
3. Import the old pet conversation into Sage, or start Sage's memory clean?
4. Screens: may the pet describe what is on your screens, or only the desk and room?
5. Addressing: wake word, "was that meant for me?" model, or always listening?
