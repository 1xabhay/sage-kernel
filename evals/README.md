# What Sage does: the behaviour suite

`behaviour.json` is one table of named cases, 40 at most. Each row is one thing Sage does, on a
plane of engagement (`senses`, `situation`, `world`, `memory`, `body`, `timing`) or under an owner
rule (`quiet` when not addressed, `no-guilt`, `no-sentience`, `no-identifying` other people,
`short` and sparing, `honest` about what it cannot do, `unknown-command` gets feedback).

A row reads: **given** this, **when** that happens, **expect** this. Its `tier` says who runs it:

| tier | runs | where |
|---|---|---|
| `ci-fake` | every commit, no model | `level: kernel` in `tests/test_behaviour.py`, `level: voice` in `devices/reachy-mini/apps/reachy_sage_voice/tests/test_behaviour.py`; the test is named `test_bNN_...` |
| `local-model` | a person, on the serial lane, against Ollama | the row's `run` |
| `on-robot` | a person, with the robot | the row's `run` |

`status` (absent means it works) and `why`:

- `not-built`: the behaviour, or its eval, does not exist yet. A `ci-fake` one is a strict expected failure.
- `bug`: built and wrong. Also a strict expected failure; fixing it turns the test red until the row is updated.
- `failing`, `never-run`, `no-gate`: what a person should know before running a `local-model` row.

The tests read the table: a `ci-fake` row with no test fails, a test with no row fails, and the
expected failures come from `status`. Every spoken line is invented; live speech never goes here
(live fixtures stay in `evals/local/`, untracked).

`companion.py` scores the conversation design on a journal's own lines (counts only, the lines are never
printed): `python3 evals/companion.py <journal.sqlite3> --hours 96` needs no model and may run any time;
with `--model <name>` it answers the lines again and holds the result to the design's bars.

`memory.py` scores memory: `python3 evals/memory.py sessions <journal.sqlite3>` counts the conversations
the kernel's rule makes of a journal's lines, and the share the writer would be shown (no model, counts only);
`python3 evals/memory.py writer --model <name>` runs the writer on the invented conversations of `memory.json`.

Never run `evals/` with a model, or `bench/`, while Sage is live: they load a model into the same Ollama.
