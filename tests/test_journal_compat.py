"""A journal written by an earlier kernel opens unchanged under per-device lanes.

Set SAGE_COMPAT_JOURNAL to a backup of a real journal; skipped otherwise. The test works on a copy in a
temporary folder and never opens the original. A real journal holds private conversation, so only counts
and invariants are asserted, and none of its content is printed.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from sage import policy
from sage.journal import Journal
from sage.kernel import Kernel

MANIFEST = {"senses": ("speech",), "tools": ()}


class EarlierJournal(unittest.TestCase):
    def setUp(self):
        source = os.environ.get("SAGE_COMPAT_JOURNAL")
        if not source:
            self.skipTest("SAGE_COMPAT_JOURNAL names no journal to copy")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "kernel.sqlite3"
        for end in ("", "-wal", "-shm"):  # the WAL holds the latest commits
            if Path(source + end).exists():
                shutil.copyfile(source + end, str(self.path) + end)

    def rows(self, through=None):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute("SELECT * FROM journal WHERE sequence <= coalesce(?, sequence) "
                                      "ORDER BY sequence", (through,)).fetchall()

    def system(self, state, device):
        return policy.compile_prompt(policy.focus(state, device), device, MANIFEST).system

    def test_identity_facts_and_old_rows_survive_a_second_body_s_turn(self):
        journal = Journal(self.path)
        old, head = journal.state, journal.head
        old_rows = self.rows(head)
        self.assertGreater(len(old_rows), 0)
        self.assertGreater(len(old.facts), 0, "a journal with no facts proves little")
        self.assertEqual(policy.decode(json.loads(json.dumps(policy.encode(old)))), old)
        device = old.turn.source or next((x.device for x in reversed(old.exchanges) if x.device), "reachy")
        before = self.system(old, device)
        self.assertIn("You are Sage", before)
        self.assertTrue(all(fact in before for fact in old.facts))

        def decide(prompt, heard):
            return json.dumps({"reply": "Hello from the phone."})
        kernel = Kernel(journal, decide)
        kernel.hello("phone", {"protocols": [1], "manifest": {"senses": [], "tools": []}})
        kernel.event("phone", {"kind": "submitted", "turn_id": "compat-p1", "text": "Hi", "input": {"medium": "text"}})
        deadline = time.monotonic() + 5
        while policy.lane(kernel.state, "phone").status == "active" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(policy.lane(kernel.state, "phone").status, "completed")

        again = Journal(self.path).state  # what the next start reads
        self.assertEqual(self.rows(head), old_rows)  # appended to, never rewritten
        self.assertEqual(again.facts, old.facts)
        self.assertEqual(self.system(again, device), before)  # identity and facts, byte for byte
        self.assertEqual(policy.lane(again, device).turn_id, old.turn.turn_id)
        self.assertEqual(again.exchanges[-1].device, "phone")
        self.assertIn(policy.lane(again, device).status, {old.turn.status, "unknown"})


if __name__ == "__main__":
    unittest.main()
