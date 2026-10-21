"""The SQLite journal and checkpoint: what the kernel keeps across a crash."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from sage import policy
from sage.journal import Journal
from sage.policy import Companion, Row


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
        self.assertEqual(again.deliveries("reachy", 0, 2, 105.0, 10),
                         [{"sequence": 2, "kind": "reply", "turn_id": "r1", "text": "Hello.", "stale": False}])
        self.assertEqual(again.deliveries("reachy", 0, 2, 111.0, 10)[0]["stale"], True)
        self.assertEqual(again.deliveries("mock", 0, 2, 105.0, 10), [])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_a_step_commits_whole_or_not_at_all(self):
        journal = Journal(self.path)
        rows = (Row("decided", "kernel", "r1"), Row("completed", None, "r1"))  # the second insert fails
        with self.assertRaises(sqlite3.IntegrityError):
            journal.append(rows, Companion(), 100.0)
        again = Journal(self.path)
        self.assertEqual((journal.head, again.head, again.state), (0, 0, Companion()))


if __name__ == "__main__":
    unittest.main()
