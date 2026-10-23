"""bench/latency.py: where a live turn's time goes, read from a journal it must never change."""

import contextlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from bench import latency
from sage.journal import Journal
from sage.kernel import Kernel


class Timed(str):
    """A model answer with the model server's own timing, as the Ollama provider returns it."""


class LatencyReport(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "kernel.sqlite3"
        clock, queue = [1000.0], [("Hi! Nice to meet you.", 1.5), ("Hi! Nice to meet you.", 2.0),
                                  ("Lisbon is lovely in spring.", 2.0)]

        def decide(prompt, heard):
            reply, seconds = queue.pop(0)
            clock[0] += seconds  # the model's time, as the kernel's clock sees it
            answer = Timed(json.dumps({"reply": reply}))
            answer.timing = {"prompt_tokens": 2337, "prefill_ms": 450, "gen_tokens": 36, "decode_ms": 1217,
                             "load_ms": 0} if len(queue) == 2 else None  # r1's call only
            return answer
        self.kernel = kernel = Kernel(Journal(self.path), decide, clock=lambda: clock[0])
        kernel.hello("reachy", {"protocols": [1], "manifest": {"senses": ["speech"], "tools": []}})
        self.turn(kernel, "r1", "Hello there, Sage", {"stt_ms": 300})
        reply = next(e for e in kernel.effects("reachy", 0, 0, 0)["effects"] if e["kind"] == "reply")
        kernel.event("reachy", {"kind": "result", "sequence": reply["sequence"], "status": "done",
                                "timing": {"ttft_ms": 900, "first_audio_ms": 1400}, "duration_ms": 2000})
        kernel.event("reachy", {"kind": "notes", "topic": "Lisbon", "passages": ["Lisbon is Portugal's capital."]})
        self.turn(kernel, "r2", "Tell me about Lisbon", {})  # repeats r1 first: one retry

    def turn(self, kernel, turn_id, text, timing):
        kernel.event("reachy", {"kind": "submitted", "turn_id": turn_id, "text": text, "timing": timing})
        deadline = time.monotonic() + 5
        while kernel.state.turn.status == "active" and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_each_turn_shows_kernel_time_retries_prompt_size_notes_and_device_timing(self):
        r1, r2 = latency.turns(self.path)
        self.assertEqual((r1["turn_id"], r1["text"], r1["decided_s"], r1["retries"], r1["notes"]),
                         ("r1", "Hello there, Sage", 1.5, 0, False))
        self.assertEqual((r1["stt_ms"], r1["ttft_ms"], r1["first_audio_ms"]), (300, 900, 1400))
        self.assertEqual((r2["turn_id"], r2["decided_s"], r2["retries"], r2["notes"]), ("r2", 4.0, 1, True))
        self.assertEqual((r2["stt_ms"], r2["ttft_ms"], r2["first_audio_ms"]), (None, None, None))
        self.assertEqual((r1["prefill_ms"], r1["decode_ms"], r2["prefill_ms"], r2["decode_ms"]), (450, 1217, None, None))
        self.assertGreater(r2["prompt_chars"], r1["prompt_chars"])
        self.assertGreater(r1["prompt_chars"], 1000)  # the persona alone
        self.assertEqual([t["turn_id"] for t in latency.turns(self.path, since=1001.0)], ["r2"])

    def test_it_prints_a_line_per_turn_and_a_p50_p95_summary(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            latency.main([str(self.path)])
        lines = out.getvalue().splitlines()
        self.assertTrue(any(line.startswith("r1 ") for line in lines), lines)
        self.assertTrue(any(line.startswith("r2 ") and "notes" in line for line in lines), lines)
        self.assertTrue(any("p50" in line and "p95" in line for line in lines), lines)
        self.assertIn("prefill_ms: p50 450ms p95 450ms (n=1)", lines)
        self.assertIn("decode_ms: p50 1217ms p95 1217ms (n=1)", lines)
        self.assertEqual((latency.pct(range(1, 21), 50), latency.pct(range(1, 21), 95)), (10, 19))

    def test_the_journal_is_opened_read_only_and_left_unchanged(self):
        before = self.path.read_bytes()
        with contextlib.closing(latency.connect(self.path)) as connection, self.assertRaises(sqlite3.OperationalError):
            connection.execute("DELETE FROM journal")
        latency.turns(self.path)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
