"""Memory, scored offline (design 3-memory §4). Two commands; both print counts only, never a heard line.

    python3 evals/memory.py sessions <journal.sqlite3> [--idle 300] [--cap 40] [--hours N]

How a journal's heard lines fall into conversations under the kernel's rule (policy.rest: closed `--idle` seconds
after the last line said to Sage, or at `--cap` such lines), against one window that never ends; and what share of
the lines the writer would be shown (policy.memorable). No model: it may run any time. A journal from before the
body's marks (protocol v1.6) has none, so its lines are replayed through the body's gate, without the confidence,
face and TV signals the journal never kept: the gate then answers more than it would live, and the conversations
come out longer than they will be.

    python3 evals/memory.py writer --model qwen3:4b-instruct [--url http://127.0.0.1:11434]

What the writer keeps, on the scripted conversations of evals/memory.json (every line invented), through the
kernel's own checks, held to BARS. It loads a model into the same Ollama: never while Sage is live.
"""

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sage import policy

# The design's pass bars (§4). ponytail: guesses until the first run with a model.
KEEPS, WRONG_PER_SESSION = 0.80, 0.10
SCRIPTED = Path(__file__).with_name("memory.json")


def heard(path: Path, hours: float | None = None) -> list[tuple[float, dict]]:
    """The journal's heard lines, oldest first: (when, the submitted row's body with `replied`: when its reply
    was delivered and its text, or None, for the gate's replay). Opened read-only."""
    with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as journal:
        newest = journal.execute("SELECT max(at) FROM journal").fetchone()[0] or 0
        rows = journal.execute("SELECT kind, turn_id, body, at FROM journal WHERE at >= ? AND kind IN "
                               "('submitted', 'reply') ORDER BY sequence", (newest - hours * 3600 if hours else 0,)).fetchall()
    replies = {turn_id: (at, json.loads(body).get("text", "")) for kind, turn_id, body, at in rows if kind == "reply"}
    return [(at, {**json.loads(body), "replied": replies.get(turn_id)}) for kind, turn_id, body, at in rows
            if kind == "submitted"]


def gated(lines) -> list[tuple[float, dict]] | None:
    """`lines` with the mark the body's gate would have given each, for a journal that has none. None when the
    gate is not here to ask (the kernel's image holds no device code)."""
    sys.path.insert(0, str(ROOT / "devices" / "common" / "sage_device"))
    try:
        from sage_device import gate
    except ImportError:
        return None
    name = policy.NAME.lower()
    state, spoke, asked, out = gate.Attention(), -math.inf, False, []
    for at, body in lines:
        signals = gate.Signals(now=at, spoke_at=spoke, asked=asked and at - spoke < gate.CLOSE_S,
                               named=lambda word: word.removesuffix("'s") == name)
        outcome, _, state = gate.attend(body["text"], signals, state)
        if outcome == gate.ANSWER and body["replied"]:
            spoke, asked = body["replied"][0], body["replied"][1].rstrip().endswith("?")
        out.append((at, {**body, "addressed": outcome == gate.ANSWER}))
    return out


def conversations(times, idle: float, cap: int | None) -> list[int]:
    """How many lines each conversation holds, the lines said to Sage at `times`: policy.rest and submit's cap."""
    lengths, last = [], None
    for at in times:
        if last is None or at - last > idle or (cap and lengths[-1] >= cap):
            lengths.append(0)
        lengths[-1] += 1
        last = at
    return lengths


def _spread(lengths) -> dict:
    ordered = sorted(lengths)

    def at(q: float) -> int:
        return ordered[min(len(ordered) - 1, int(len(ordered) * q))] if ordered else 0
    return {"conversations": len(ordered), "median": at(0.5), "p90": at(0.9), "longest": at(1)}


def sessions(lines, idle: float = policy.SESSION_IDLE_S, cap: int = policy.SESSION_TURNS) -> dict:
    """The counts: every heard line as one never-ending window, then cut by pauses alone (what a silence rule over
    all lines would give), then the kernel's rule over the lines said to Sage, and the writer's share."""
    said = [(at, body) for at, body in lines if body.get("addressed") or (body.get("input") or {}).get("medium") == "text"]
    ruled = conversations([at for at, _ in said], idle, cap)
    shown = [bool(policy.memorable({**body, "addressed": True})) for _, body in said]
    read, start = 0, 0
    for length in ruled:  # a conversation with no line for the writer asks no model
        read += any(shown[start:start + length])
        start += length
    return {"heard_lines": len(lines), "today_one_window": len(lines),
            "pauses_over_all_lines": _spread(conversations([at for at, _ in lines], idle, None)),
            "said_to_sage": len(said), "rule_without_cap": _spread(conversations([at for at, _ in said], idle, None)),
            "rule": {**_spread(ruled), "closed_at_cap": sum(length >= cap for length in ruled),
                     "one_line": sum(length == 1 for length in ruled)},
            "writer_lines": sum(shown), "writer_share_of_heard": round(sum(shown) / len(lines), 3) if lines else 0.0,
            "writer_calls": read}


def score(write, cases) -> dict:
    """Each scripted conversation through the kernel's own filter (policy.memorable), `write` and the kernel's
    own checks (policy.written): what was kept against what must and must never be."""
    counts = {"sessions": len(cases), "must_keep": 0, "kept": 0, "wrong": 0, "refused": 0, "operations": 0, "failed": 0}
    for case in cases:
        facts = tuple({"id": n, "who": "owner", "kind": "person", "text": text, "at": 0.0, "from": None}
                      for n, text in enumerate(case.get("known", ()), 1))
        said = [(n, {"text": line["text"], "addressed": line.get("to", "sage") == "sage",
                     "input": {"medium": "speech", "confidence": line.get("confidence", 0.95)},
                     **({"speaker": line["speaker"]} if "speaker" in line else {})}, line.get("reply", ""))
                for n, line in enumerate(case["said"], 100)]
        lines = [(n, who, body["text"], reply) for n, body, reply in said if (who := policy.memorable(body))]
        try:
            output = write(policy.writer_prompt(facts, lines), lambda piece: None) if lines else None
        except (OSError, ValueError):
            output, counts["failed"] = None, counts["failed"] + 1
        state, rows = policy.written(policy.Companion(facts=facts, due=1), 999, lines, output, 1.0)
        kept = " | ".join(r["text"].lower() for r in state.facts if r["kind"] != "day")
        counts["operations"] += sum(row.kind in ("remember", "forget") for row in rows)
        counts["refused"] += len(rows[-1].body["refused"])
        counts["must_keep"] += len(case.get("keep", ()))
        counts["kept"] += sum(all(word in kept for word in words) for words in case.get("keep", ()))
        counts["wrong"] += sum(word in kept for word in case.get("never", ()))
    counts["keep_rate"] = round(counts["kept"] / counts["must_keep"], 3) if counts["must_keep"] else 1.0
    counts["wrong_per_session"] = round(counts["wrong"] / len(cases), 3) if cases else 0.0
    counts["passes"] = counts["keep_rate"] >= KEEPS and counts["wrong_per_session"] <= WRONG_PER_SESSION and not counts["failed"]
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("sessions")
    one.add_argument("journal", type=Path)
    one.add_argument("--idle", type=float, default=policy.SESSION_IDLE_S)
    one.add_argument("--cap", type=int, default=policy.SESSION_TURNS)
    one.add_argument("--hours", type=float)
    two = commands.add_parser("writer")
    two.add_argument("--model", required=True)
    two.add_argument("--url", default="http://127.0.0.1:11434")
    args = parser.parse_args(argv)
    if args.command == "writer":
        from sage.adapters import model
        result = score(model.writer(args.url, args.model), json.loads(SCRIPTED.read_text())["cases"])
        print(json.dumps(result, indent=1))
        return 0 if result["passes"] else 1
    lines = heard(args.journal, args.hours)
    marks = "the journal's own"
    if not any("addressed" in body for _, body in lines):
        replayed = gated(lines)
        marks = "the gate, replayed without confidence, face or TV" if replayed else "none: every line counted as said to Sage"
        lines = replayed or [(at, {**body, "addressed": True}) for at, body in lines]
    print(json.dumps({"marks": marks, "idle_s": args.idle, "cap": args.cap, **sessions(lines, args.idle, args.cap)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
