# Roadmap

What is planned, not promised. Decisions live in [DECISIONS.md](DECISIONS.md);
what has shipped is in [CHANGELOG.md](../CHANGELOG.md).

## Protocol

- **Freeze v1** (Draft to Stable, [PROTOCOL.md](PROTOCOL.md#status-and-versioning))
  once emote and presence have run through it on a real device. Owner decision.
- **A device-side conformance suite**, so a body can be checked against the
  protocol the way a kernel is today, as the Model Context Protocol tests both
  clients and servers ([MCP conformance](https://modelcontextprotocol.io/community/sdk-tiers)).

## Journal export as a ConvoKit corpus

Not built yet. The journal could be exported in the corpus format of
[ConvoKit](https://convokit.cornell.edu/) ([data format](https://convokit.cornell.edu/documentation/data_format.html)):
`utterances.jsonl` (`id`, `speaker`, `conversation_id`, `reply_to`,
`timestamp`, `text`, `meta`), `speakers.json`, `conversations.json`,
`corpus.json` and `index.json`. An earlier review found that it maps cleanly
except for three gaps. The journal's columns (`sequence`, `kind`, `source`,
`turn_id`, `body`, `at`) give `id`, `speaker`, `text` and `timestamp`, a
reply's `turn_id` gives `reply_to`, and the rest of `body` can go in `meta`.
The gaps the export would have to fill:

- **Conversation id.** The journal has turns, not conversations; an export
  needs a rule for where one conversation ends (for example the disposition's
  absence and quiet timers).
- **Per-person speakers.** Every submitted line comes from a device, not a
  person (v1 assumes one person per device); the speaker would be the device
  until the protocol carries who spoke.
- **Interrupted replies.** The journal keeps only what was played; ConvoKit
  has no notion of a cut-off utterance, so that needs a `meta` field.
