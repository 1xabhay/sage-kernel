"""The SQLite journal and checkpoint: what the kernel keeps across a crash."""

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
from sage.policy import Companion, Row

# A checkpoint as a kernel before lanes, acts and input wrote it (it still kept `repeat`), with one fact.
EARLIER = {"turn": {"turn_id": "r1", "source": "reachy", "status": "completed", "text": "", "timing": {}},
           "open": [], "facts": ["Their name is Juno."], "exchanges": [
               {"user": "My name is Juno.", "reply": "Hi Juno!", "reply_sequence": 2, "repeat": False, "calls": []}]}


class JournalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state" / "kernel.sqlite3"

    def test_rows_and_state_survive_a_reopen_and_sequence_only_grows(self):
        journal = Journal(self.path)
        state, rows = policy.submit(Companion(), "reachy", "r1", "Hi", {}, seen=False)
        journal.append(rows + (Row("reply", "kernel", "r1", {"text": "Hello."}, target="reachy"),), state, 100.0)
        again = Journal(self.path)
        self.assertEqual((again.head, again.state), (2, state))
        self.assertTrue(again.seen("reachy", "r1"))
        self.assertFalse(again.seen("mock", "r1"))
        self.assertEqual(again.effect(2), {"sequence": 2, "kind": "reply", "target": "reachy", "turn_id": "r1"})
        self.assertIsNone(again.effect(1))  # an event is not a delivered effect
        self.assertEqual(again.reply_of("reachy", "r1"), again.effect(2))
        self.assertIsNone(again.reply_of("mock", "r1"))
        self.assertEqual(again.deliveries("reachy", 0, 2, 110.0, 10),  # fresh for 10 s by the kernel's clock
                         [{"sequence": 2, "kind": "reply", "turn_id": "r1", "text": "Hello.", "stale": False}])
        self.assertEqual(again.deliveries("reachy", 0, 2, 110.01, 10)[0]["stale"], True)
        self.assertEqual(again.deliveries("mock", 0, 2, 105.0, 10), [])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_a_step_commits_whole_or_not_at_all(self):
        journal = Journal(self.path)
        rows = (Row("decided", "kernel", "r1"), Row("completed", None, "r1"))  # the second insert fails
        with self.assertRaises(sqlite3.IntegrityError):
            journal.append(rows, Companion(), 100.0)
        again = Journal(self.path)
        self.assertEqual((journal.head, again.head, again.state), (0, 0, Companion()))


class EarlierJournal(unittest.TestCase):
    """A journal written by an earlier kernel opens unchanged under per-device lanes. SAGE_COMPAT_JOURNAL names a
    backup of a real one to check instead of the written one: it is copied, never opened in place, and holds
    private conversation, so only counts and invariants are asserted and none of its content is printed."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "kernel.sqlite3"
        if source := os.environ.get("SAGE_COMPAT_JOURNAL"):
            for end in ("", "-wal", "-shm"):  # the WAL holds the latest commits
                if Path(source + end).exists():
                    shutil.copyfile(source + end, str(self.path) + end)
            return
        Journal(self.path)  # the tables; the rows and the checkpoint are the earlier kernel's
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executemany("INSERT INTO journal VALUES (?, ?, ?, ?, ?, ?, ?)", [
                (1, "submitted", "reachy", None, "r1", '{"text": "My name is Juno.", "timing": {}}', 100.0),
                (2, "reply", "kernel", "reachy", "r1", '{"text": "Hi Juno!"}', 101.0),
                (3, "completed", "kernel", None, "r1", "{}", 101.0)])
            connection.execute("INSERT INTO checkpoint VALUES (1, ?)", (json.dumps(EARLIER),))

    def rows(self, through):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute("SELECT * FROM journal WHERE sequence <= ? ORDER BY sequence",
                                      (through,)).fetchall()

    def system(self, state, device):
        return policy.compile_prompt(policy.focus(state, device), device, {"senses": ("speech",), "tools": ()}).system

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

        kernel = Kernel(journal, lambda prompt, heard: json.dumps({"reply": "Hello from the phone."}))
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
