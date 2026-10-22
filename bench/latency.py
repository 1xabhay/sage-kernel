"""Where a live turn's time goes, turn by turn, from the kernel's journal (opened read-only: it is the agent's memory).

Per submitted turn: kernel time from submit to the decided (or failed) row, model retries (rejected rows), the
size of the prompt the model saw, whether notes rode on the newest message, and what the device reported
(stt_ms, ttft_ms, first_audio_ms; docs/PROTOCOL.md). Submit to first reply_delta is not journaled.

    python bench/latency.py <kernel.sqlite3> [--since-minutes N]
"""

import argparse
import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path

DEVICE = ("stt_ms", "ttft_ms", "first_audio_ms")


def connect(path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def turns(path, since: float | None = None) -> list[dict]:
    with closing(connect(path)) as connection:
        rows = connection.execute(
            "SELECT kind, turn_id, body, at FROM journal WHERE turn_id IS NOT NULL AND kind IN "
            "('submitted', 'rejected', 'decided', 'failed', 'result') ORDER BY sequence").fetchall()
    found = {}
    for kind, turn_id, body, at in rows:
        body = json.loads(body)
        if kind == "submitted":
            if turn_id not in found and (since is None or at >= since):
                found[turn_id] = {"turn_id": turn_id, "text": body["text"], "submitted_at": at, "decided_s": None,
                                  "outcome": None, "retries": 0, "prompt_chars": None, "notes": None,
                                  **{key: body.get("timing", {}).get(key) for key in DEVICE}}
            continue
        turn = found.get(turn_id)
        if turn is None:
            continue
        if kind == "rejected":
            turn["retries"] += 1
        elif kind in ("decided", "failed") and turn["outcome"] is None:
            prompt = body["prompt"]
            newest = prompt["context"][-1]["content"] if prompt["context"] else ""
            turn |= {"outcome": kind, "decided_s": round(at - turn["submitted_at"], 3), "notes": "[Notes on" in newest,
                     "prompt_chars": len(prompt["system"]) + sum(len(m["content"]) for m in prompt["context"])}
        elif kind == "result":
            for key in DEVICE:
                turn[key] = turn[key] if turn[key] is not None else (body.get("timing") or {}).get(key)
    return list(found.values())


def pct(values, p: float):
    """Nearest-rank percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def _ms(value) -> str:
    return "-" if value is None else str(value)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("journal")
    parser.add_argument("--since-minutes", type=float)
    args = parser.parse_args(argv)
    since = None if args.since_minutes is None else time.time() - args.since_minutes * 60
    found = turns(args.journal, since)
    for t in found:
        decided = "-" if t["decided_s"] is None else f"{t['decided_s']:.2f}s"
        prompt = "-" if t["prompt_chars"] is None else f"{t['prompt_chars']}ch~{t['prompt_chars'] // 4}tok"
        print(f"{t['turn_id']} {t['text'][:30]!r} {t['outcome'] or 'pending'} {decided} retries {t['retries']} "
              f"prompt {prompt} notes {'yes' if t['notes'] else 'no'} "
              + " ".join(f"{key[:-3]} {_ms(t[key])}" for key in DEVICE))
    print(f"{len(found)} turns, {sum(t['retries'] for t in found)} retries, "
          f"{sum(bool(t['notes']) for t in found)} with notes")
    for key, unit in (("decided_s", "s"), ("prompt_chars", "ch"), *((key, "ms") for key in DEVICE)):
        values = [t[key] for t in found if t[key] is not None]
        if values:
            print(f"{key}: p50 {pct(values, 50)}{unit} p95 {pct(values, 95)}{unit} (n={len(values)})")


if __name__ == "__main__":
    main()
