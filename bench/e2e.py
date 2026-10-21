"""The chained compute path of one voice turn, measured, not summed.

For each synthetic clip (stt.py, voice bf_emma, 20 lines): Parakeet transcribes it,
the transcript goes to Ollama with the format_best harness (llm.py), and the
moment the reply's first clause is complete Kokoro int8 synthesises that clause.
Clock: clip handed to STT -> first clause's audio ready.

Not included (added in LEDGER.md's budget): the end-of-speech silence wait
before the clip is handed over, and audio transport/playback to the robot.

    uv run python e2e.py [model ...]
"""
import json
import re
import statistics
import sys
import time

import llm
import stt
import tts


def first_clause(model, text):
    """(reply's first clause, time it was complete, emotion) from a streamed format_best turn."""
    b = llm.base(model, llm.GLOSS_SYS, text, "1h" if model == "qwen2.5:7b" else "3m")
    b |= {"format": llm.SCHEMA_BEST, "stream": True}
    content = ""
    with llm.call(b) as r:
        for line in r:
            content += json.loads(line).get("message", {}).get("content", "")
            m = re.search(r'"reply"\s*:\s*"([^"]*?[,.!?])', content) or re.search(r'"reply"\s*:\s*"([^"]*)"', content)
            if m and m.group(1).strip():
                return m.group(1)  # ponytail: stops reading here; the rest of the reply is not timed
    return content


def main(models):
    asr, k = stt.parakeet(), tts.kokoro()
    data = [c for c in stt.clips() if c[1] == "bf_emma"]
    k.create("Warm up.", voice="af_heart")
    for model in models:
        first_clause(model, "hello")  # warm
        rows = []
        for ref, _, audio in data:
            t0 = time.perf_counter()
            heard = asr(audio)
            t1 = time.perf_counter()
            clause = first_clause(model, heard)
            t2 = time.perf_counter()
            k.create(clause, voice="af_heart")
            t3 = time.perf_counter()
            rows.append((t1 - t0, t2 - t1, t3 - t2, t3 - t0, clause))
        col = lambda i: [r[i] for r in rows]
        print(json.dumps({"model": model, "n": len(rows),
                          **{f"{k}_{p}": round(statistics.median(col(i)) if p == "p50" else stt.pct(col(i), 95), 3)
                             for i, k in enumerate(("stt", "llm_clause", "tts_clause", "compute")) for p in ("p50", "p95")},
                          "clauses": [r[4] for r in rows[:5]]}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:] or ("qwen3:1.7b", "qwen2.5:7b"))
