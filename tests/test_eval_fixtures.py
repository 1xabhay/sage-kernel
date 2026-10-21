"""The eval's fixtures: written ones in the tree, live ones (the owner's real speech) only in evals/local/."""

import json
import tempfile
import unittest
from pathlib import Path

from evals.test_live_conversation import fixture
from sage import policy

ROOT = Path(__file__).resolve().parents[1]


class FixtureTests(unittest.TestCase):
    def test_a_live_fixture_in_local_is_read_before_the_written_one(self):
        with tempfile.TemporaryDirectory() as directory:
            here = Path(directory)
            (here / "case.json").write_text('{"from": "written"}')
            self.assertEqual(fixture("case.json", here), {"from": "written"})
            (here / "local").mkdir()
            (here / "local" / "case.json").write_text('{"from": "live"}')
            self.assertEqual(fixture("case.json", here), {"from": "live"})

    def test_the_written_fixtures_exercise_every_check_the_live_ones_do(self):
        first = json.loads((ROOT / "evals" / "first_run.json").read_text())
        self.assertEqual(len(first["lines"]), 5)
        self.assertLessEqual(set(first["may_quote"]), set(first["lines"]))
        self.assertIn("emote", [tool["name"] for tool in first["manifest"]["tools"]])
        run = json.loads((ROOT / "evals" / "run_5b.json").read_text())
        self.assertLessEqual(set(run["barge_in"]), set(run["lines"]))
        self.assertGreaterEqual(len(run["assistant_lines"]), 1)
        name = run["name"]
        self.assertIn(name["intro"], run["lines"])
        self.assertIn(name["name"].lower(), name["intro"].lower())
        for wrong in name["not"]:  # each mishearing of the name was said back to the robot
            self.assertTrue(any(wrong in line for line in run["lines"]), wrong)
        later = run["run_4b"]
        self.assertLessEqual(set(later["lyrics"]) | set(later["fragments"]), set(later["lines"]))
        for line in later["lyrics"] + later["fragments"]:
            with self.subTest(line=line):
                self.assertTrue(policy.misheard(line))

    def test_no_live_fixture_is_tracked_outside_local(self):
        for path in (ROOT / "evals").glob("*.json"):
            with self.subTest(path=path.name):
                self.assertFalse(path.name.startswith("live_"))
                self.assertTrue(json.loads(path.read_text())["source"].startswith("written"))


if __name__ == "__main__":
    unittest.main()
