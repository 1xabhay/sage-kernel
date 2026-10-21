"""The SQLite journal and checkpoint. Each step's rows and the state they lead to commit together.

The journal sequence is every device's cursor; deliveries are derived from it, so there is no outbox.
"""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from sage import policy
from sage.policy import Companion

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    sequence INTEGER PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL, target TEXT,
    turn_id TEXT, body TEXT NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS deliveries ON journal (target, sequence) WHERE target IS NOT NULL;
CREATE INDEX IF NOT EXISTS submissions ON journal (source, turn_id) WHERE kind = 'submitted' AND target IS NULL;
CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT NOT NULL);
"""


class Journal:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        path.chmod(0o600)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            row = connection.execute("SELECT state FROM checkpoint WHERE id = 1").fetchone()
            self.head = connection.execute("SELECT coalesce(max(sequence), 0) FROM journal").fetchone()[0]
        self.state = policy.decode(json.loads(row[0])) if row else Companion()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def append(self, rows, state: Companion, now: float) -> None:
        """Commit `rows` and `state` in one transaction, or nothing. The caller serializes writers."""
        rows = policy.numbered(rows, self.head + 1)
        values = [(row.sequence, row.kind, row.source, row.target, row.turn_id, json.dumps(row.body), now)
                  for row in rows]
        checkpoint = json.dumps(policy.encode(state))
        with closing(self._connect()) as connection, connection:
            connection.executemany("INSERT INTO journal VALUES (?, ?, ?, ?, ?, ?, ?)", values)
            connection.execute("INSERT OR REPLACE INTO checkpoint VALUES (1, ?)", (checkpoint,))
        self.head, self.state = self.head + len(rows), state

    def seen(self, source: str, turn_id: str) -> bool:
        """Whether this device already submitted this turn ID, at any time."""
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT 1 FROM journal WHERE kind = 'submitted' AND target IS NULL AND source = ? AND turn_id = ?",
                (source, turn_id)).fetchone() is not None

    def effect(self, sequence: int) -> dict | None:
        """The delivered effect at `sequence`, or None if that row is not one."""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT sequence, kind, target, turn_id FROM journal "
                                     "WHERE sequence = ? AND target IS NOT NULL", (sequence,)).fetchone()
        return dict(zip(("sequence", "kind", "target", "turn_id"), row)) if row else None

    def reply_of(self, target: str, turn_id: str) -> dict | None:
        """The reply delivered to `target` for `turn_id`, or None."""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT sequence, kind, target, turn_id FROM journal WHERE kind = 'reply' "
                                     "AND target = ? AND turn_id = ? ORDER BY sequence DESC LIMIT 1",
                                     (target, turn_id)).fetchone()
        return dict(zip(("sequence", "kind", "target", "turn_id"), row)) if row else None

    def deliveries(self, target: str, since: int, head: int, now: float, fresh_s: float) -> list[dict]:
        """Effects delivered to `target` in (since, head], stale by the kernel's clock."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT sequence, kind, turn_id, body, at FROM journal WHERE target = ? "
                "AND sequence > ? AND sequence <= ? ORDER BY sequence", (target, since, head)).fetchall()
        return [{"sequence": sequence, "kind": kind, "turn_id": turn_id, **json.loads(body),
                 "stale": policy.stale(at, now, fresh_s)} for sequence, kind, turn_id, body, at in rows]
