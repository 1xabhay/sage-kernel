"""Does a body with moves answer like a pet? Replays real transcripts through the kernel against real Ollama, first
with the old prompt (the manifest without moves), then with moves, and checks docs/PROTOCOL.md "Moves" on a model.

One transcript a line, as one conversation, on a throwaway journal. Exits 1 if a threshold fails.

    python bench/quiet.py <lines.txt> --model <name> [--url http://127.0.0.1:11434] [--limit N]

A .jsonl of {"text", "to_sage"} (a labelled evening, kept out of git) is heard as speech, as the robot sends it,
and scores how often Sage spoke to a line no one said to it.
"""

import argparse
import json
import math
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sage import policy  # noqa: E402
from sage.adapters import model  # noqa: E402
from sage.journal import Journal  # noqa: E402
from sage.kernel import Kernel  # noqa: E402

MAX_QUESTIONS, MIN_MOVES, MAX_SILENT, MAX_FIRST_PIECE_MS = 0.10, 0.40, 0, 100  # (owner, 2026-11-05)


def p95(values) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)] if ordered else 0


def score(turns: list[dict]) -> dict:
    """The aggregates of turns {line, reply, tools, first_ms}: first_ms is None when no words were written."""
    spoken = [t for t in turns if t["reply"]]
    def needs(t):  # a labelled line was heard as speech: cut off, it needs no words
        return policy.needs_words(t["line"], {"medium": "speech"} if "to_sage" in t else None)
    statements = [t for t in turns if not needs(t)]
    must = [t for t in turns if needs(t)]
    return {"turns": len(turns),
            "question_ending": sum(t["reply"].rstrip().endswith("?") for t in spoken) / max(1, len(spoken)),
            "movement_only": sum(not t["reply"] and bool(t["tools"]) for t in statements) / max(1, len(statements)),
            "silent_on_must_speak": sum(not t["reply"] for t in must),
            "last_resort": sum(t["reply"] in (policy.QUESTION_LINE, policy.DISTRESS_LINE) for t in must),
            "first_piece_p95_ms": p95([t["first_ms"] for t in turns if t["first_ms"] is not None]),
            "noise": sum(t.get("to_sage") is False for t in turns),
            "spoke_to_noise": sum(t.get("to_sage") is False and bool(t["reply"]) for t in turns)}


def failures(before: dict, after: dict) -> list[str]:
    """The thresholds `after` misses; `before` is the old prompt on the same lines, the same day."""
    checks = ((after["question_ending"] <= MAX_QUESTIONS, f"question-ending {after['question_ending']:.0%} > {MAX_QUESTIONS:.0%}"),
              (after["movement_only"] >= MIN_MOVES, f"movement-only {after['movement_only']:.0%} < {MIN_MOVES:.0%}"),
              (after["silent_on_must_speak"] <= MAX_SILENT, f"{after['silent_on_must_speak']} silent on a question or distress"),
              (after["first_piece_p95_ms"] - before["first_piece_p95_ms"] <= MAX_FIRST_PIECE_MS,
               f"first piece p95 {after['first_piece_p95_ms']} ms against {before['first_piece_p95_ms']} ms"))
    return [why for ok, why in checks if not ok]


def replay(lines: list, declared: dict, decide) -> list[dict]:
    """Each line a str (typed, unlabelled) or {"text", "to_sage"} (heard as speech, labelled)."""
    with tempfile.TemporaryDirectory() as directory:
        started, first = [0.0], [None]

        def timed(prompt, heard):
            def piece(text):
                heard(text)
                if first[0] is None and kernel.written.get("bench"):
                    first[0] = round((time.monotonic() - started[0]) * 1000)
            return decide(prompt, piece)

        kernel, done, cursor = Kernel(Journal(Path(directory) / "k.sqlite3"), timed), [], 0
        kernel.hello("bench", {"protocols": [1], "manifest": declared})

        def send(event: dict) -> None:  # a quiet move ends a turn at once, faster than a device may send
            while True:
                try:
                    return kernel.event("bench", event)
                except policy.Refused as refused:
                    if refused.code != "rate_limited":
                        raise
                    time.sleep(0.2)
        for n, item in enumerate(lines):
            line, label = (item, {}) if isinstance(item, str) else (item["text"], {"to_sage": item["to_sage"]})
            started[0], first[0] = time.monotonic(), None
            send({"kind": "submitted", "turn_id": f"q{n}", "text": line,
                  **({"input": {"medium": "speech"}} if label else {})})
            while policy.lane(kernel.state, "bench").status == "active":
                time.sleep(0.01)
            effects = kernel.effects("bench", cursor, 0, 0)
            cursor = effects["sequence"]
            for effect in effects["effects"]:  # performed at once: a timeout would be told to the model as a failure
                send({"kind": "result", "sequence": effect["sequence"], "status": "done"})
            exchange = kernel.state.exchanges[-1]
            done.append({"line": line, "reply": exchange.reply, "tools": [call.name for call in exchange.calls],
                         "first_ms": first[0], **label})
            print(json.dumps(done[-1]), file=sys.stderr)
        return done


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("lines", type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:11434")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    rows = [line.strip() for line in args.lines.read_text().splitlines() if line.strip()]
    lines = [json.loads(row) if args.lines.suffix == ".jsonl" else row for row in rows][:args.limit]
    moving = json.loads((ROOT / "tests" / "voice_manifest.json").read_text())
    old = {key: value for key, value in moving.items() if key not in ("moves", "output")}
    decide = model.provider(args.url, args.model)
    before, after = score(replay(lines, old, decide)), score(replay(lines, moving, decide))
    print(json.dumps({"old prompt": before, "moves": after}, indent=1))
    if missed := failures(before, after):
        sys.exit("FAILED: " + "; ".join(missed))
    print("PASSED")


if __name__ == "__main__":
    main()
