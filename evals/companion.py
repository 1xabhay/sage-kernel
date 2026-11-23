"""Does Sage talk like a companion? The score of the conversation design, on a journal's own lines.

Three columns, each the same measures over the same heard lines, in order, as one conversation. The lines are
the ones the recording kernel asked its model about: the nearest the journal has to "said to Sage", until the
body's gate decides that (--all: every heard line).

    recorded      what the kernel that wrote the journal said (no model, no kernel: the rows as they are)
    new guards    the recorded answers put through this kernel, on a throwaway journal: its context, its
                  notes and its guards, with the model's words as they were (no model)
    model         the lines answered again by a model, through this kernel (--model; never while Sage is live:
                  it loads a model into the same Ollama)

    python3 evals/companion.py <journal.sqlite3> [--hours 96] [--limit N]
    python3 evals/companion.py <journal.sqlite3> --model qwen3:4b-instruct [--url http://127.0.0.1:11434] [--limit 200]

The journal is opened read-only. Only counts are printed: the lines are a person's own speech and never leave
this process. With --model, exits 1 if the model's column misses a bar (BARS, the design's; guesses until the first run).
"""

import argparse
import json
import re
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sage import manifest, policy
from sage.journal import Journal
from sage.kernel import Kernel

# The design's pass bars (.local/design/2-conversation.md §4): measure -> (most or least, value).
# ponytail: guesses until the first model run; the ones only a judge can score (answered rightly, names the heard
# part) are not here.
BARS = {"dont_know": ("most", 0.15), "dont_know_twice_running": ("most", 0), "say_again_twice_running": ("most", 0),
        "questions_dropped": ("most", 0), "statements_with_words": ("least", 0.90), "question_twice_running": ("most", 0),
        "over_two_sentences": ("most", 0.02), "top_two_feelings": ("most", 0.50), "second_calls": ("most", 0.05),
        "prompt_tokens": ("most", 2000)}
CHARS_PER_TOKEN = 3.52  # the live prompt's, qwen3:4b-instruct: 11.9k characters were 3,383 tokens (journal 11-20)


def recorded(path: Path, hours: float | None = None) -> list[dict]:
    """The journal's heard lines, oldest first, each with what was answered: {line, input, output, reply, feelings,
    calls, prompt_chars, prompt_tokens}. `output` is the model's last answer, "" if it was never asked."""
    with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as journal:
        newest = journal.execute("SELECT max(at) FROM journal").fetchone()[0] or 0
        rows = journal.execute("SELECT kind, turn_id, body FROM journal WHERE at >= ? AND kind IN "
                               "('submitted', 'decided', 'rejected', 'reply', 'emote') ORDER BY sequence",
                               (newest - hours * 3600 if hours else 0,)).fetchall()
    turns: dict[str, dict] = {}
    for kind, turn_id, body in rows:
        body = json.loads(body)
        if kind == "submitted":
            turns[turn_id] = {"line": body["text"], "input": body.get("input"), "output": "", "reply": "", "feelings": [],
                              "calls": 0, "prompt_chars": None, "prompt_tokens": None}
        elif (turn := turns.get(turn_id)) is None:
            continue  # a turn the kernel started itself, or one heard before the window
        elif kind == "reply":
            turn["reply"] = body["text"]
        elif kind == "emote":
            turn["feelings"].append((body.get("input") or {}).get("name"))
        else:
            asked = kind == "rejected" or "model_timing" in body  # a model's answer is journaled with its timing
            turn["calls"] += asked
            turn["output"] = body["output"] if asked else turn["output"]
            if kind == "decided":
                prompt, timing = body["prompt"], body.get("model_timing") or {}
                turn["prompt_chars"] = len(prompt["system"]) + sum(len(m["content"]) for m in prompt["context"])
                turn["prompt_tokens"] = timing.get("prompt_tokens")
    return list(turns.values())


def replay(turns: list[dict], declared: dict, decide=None) -> list[dict]:
    """`turns` heard again by this kernel on a throwaway journal, answered by `decide`, or with no `decide` by
    each turn's recorded `output` (asked twice, the recording says the same again: the worst a model could do)."""
    done, now = [], {}

    def model(prompt, heard):
        now["calls"] += 1
        now["prompt_chars"] = len(prompt.system) + sum(len(m["content"]) for m in prompt.context)
        if decide:
            answer = decide(prompt, heard)
            now["prompt_tokens"] = (getattr(answer, "timing", None) or {}).get("prompt_tokens")
            return answer
        heard(now["output"])
        return now["output"]

    with tempfile.TemporaryDirectory() as directory:
        kernel, cursor = Kernel(Journal(Path(directory) / "k.sqlite3"), model), 0
        kernel.hello("eval", {"protocols": [1], "manifest": declared})

        def send(event: dict) -> None:
            kernel.buckets.clear()  # a recording is heard faster than a device may send
            kernel.event("eval", event)
        for n, turn in enumerate(turns):
            now = {"output": turn.get("output") or json.dumps({"reply": ""}), "calls": 0, "prompt_chars": None,
                   "prompt_tokens": None}
            send({"kind": "submitted", "turn_id": f"e{n}", "text": turn["line"],
                  **({"input": turn["input"]} if turn.get("input") else {})})
            while policy.lane(kernel.state, "eval").status == "active":
                time.sleep(0.005)
            effects = kernel.effects("eval", cursor, 0, 0)
            cursor = effects["sequence"]
            for effect in effects["effects"]:  # performed at once: a timeout would be told to the model as a failure
                send({"kind": "result", "sequence": effect["sequence"], "status": "done"})
            done.append({"line": turn["line"], "input": turn.get("input"),
                         "reply": next((e["text"] for e in effects["effects"] if e["kind"] == "reply"), ""),
                         "feelings": [e["input"].get("name") for e in effects["effects"] if e["kind"] == "emote"],
                         "calls": now["calls"], "prompt_chars": now["prompt_chars"], "prompt_tokens": now["prompt_tokens"]})
    return done


def _matches(pattern, reply: str) -> bool:
    return bool(pattern.search(policy._plain(reply)))


def _running(flags: list[bool]) -> int:
    """How many times two in a row are true."""
    return sum(a and b for a, b in pairwise(flags))


def _share(part, whole) -> float:
    return round(part / whole, 3) if whole else 0.0


def score(turns: list[dict]) -> dict:
    """The design's measures that need no judge, over turns {line, input, reply, feelings, calls, prompt_*}."""
    spoken = [t["reply"] for t in turns if t["reply"]]
    asks = [t for t in turns if policy.needs_words(t["line"], t["input"]) == policy.QUESTION_LINE]
    clear = [t for t in turns if not policy.misheard(t["line"], t["input"])]
    statements = [t for t in clear if not policy.needs_words(t["line"], t["input"])
                  and not set(policy._normal(t["line"]).split()) <= policy.BACKCHANNEL]
    feelings = Counter(name for t in turns for name in t["feelings"])
    asked = [t for t in turns if t["calls"]]
    sizes = [t["prompt_chars"] for t in asked if t["prompt_chars"]]
    counted = [t["prompt_tokens"] for t in asked if t["prompt_tokens"]]
    chars = statistics.median(sizes) if sizes else 0
    return {
        "turns": len(turns), "spoken": len(spoken),
        "dont_know": _share(sum(_matches(policy.ADMITS, r) for r in spoken), len(spoken)),
        "dont_know_twice_running": _running([_matches(policy.ADMITS, r) for r in spoken]),
        "say_again_twice_running": _running([_matches(policy.ASKS_AGAIN, r) or _matches(policy.ASKS_TO_REPEAT, r)
                                             for r in spoken]),
        "questions": len(asks),
        "questions_dropped": sum(not t["reply"] for t in asks),
        "questions_dont_know": sum(_matches(policy.ADMITS, t["reply"]) for t in asks if t["reply"]),
        "statements_with_words": _share(sum(bool(t["reply"]) for t in statements), len(statements)),
        "ends_in_question": _share(sum(r.rstrip().endswith("?") for r in spoken), len(spoken)),
        "question_twice_running": _running([r.rstrip().endswith("?") for r in spoken]),
        "reply_chars_median": statistics.median(map(len, spoken)) if spoken else 0,
        "reply_words_median": statistics.median(len(r.split()) for r in spoken) if spoken else 0,
        "over_two_sentences": _share(sum(len(re.findall(r"[.!?]+(?:\s|$)", r)) > 2 for r in spoken), len(spoken)),
        "at_the_cap": _share(sum(len(r) >= manifest.MAX_REPLY - 1 for r in spoken), len(spoken)),
        "top_two_feelings": _share(sum(n for _, n in feelings.most_common(2)), sum(feelings.values())),
        "model_turns": len(asked),
        "second_calls": _share(sum(t["calls"] > 1 for t in asked), len(asked)),
        "prompt_chars_median": chars,
        # The model server's own count when it gave one; else the live prompt's characters per token.
        "prompt_tokens": statistics.median(counted) if counted else round(chars / CHARS_PER_TOKEN),
    }


def misses(got: dict) -> list[str]:
    """The bars `got` misses."""
    return [f"{name} {got[name]} is not at {side} {bar}" for name, (side, bar) in BARS.items()
            if (got[name] > bar if side == "most" else got[name] < bar)]


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("journal", type=Path)
    parser.add_argument("--hours", type=float, help="only the last hours of the journal")
    parser.add_argument("--limit", type=int, help="only the last lines")
    parser.add_argument("--all", action="store_true", help="every heard line, not only those the model was asked about")
    parser.add_argument("--model", help="answer the lines again with this model (never while Sage is live)")
    parser.add_argument("--url", default="http://127.0.0.1:11434")
    parser.add_argument("--manifest", type=Path, default=ROOT / "tests" / "voice_manifest.json")
    args = parser.parse_args(argv)
    turns, declared = recorded(args.journal, args.hours), json.loads(args.manifest.read_text())
    turns = [turn for turn in turns if args.all or turn["calls"]][-(args.limit or 0):]
    # The recorded answers name the old body's clips, which this body does not list: their feelings are not scored.
    out = {"recorded": score(turns), "new guards": {**score(replay(turns, declared)), "top_two_feelings": None}}
    if args.model:
        from sage.adapters import model
        out["model"] = score(replay(turns, declared, model.provider(args.url, args.model)))
    print(json.dumps(out, indent=1))
    if args.model:  # the bars are for new words: the recorded ones were written for the old character
        if missed := misses(out["model"]):
            sys.exit("MISSED: " + "; ".join(missed))
        print("PASSED")


if __name__ == "__main__":
    main()
