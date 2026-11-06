"""Wave 1 eval (K7), against real Ollama: spoken commands become body plans, the body knows what it has,
and notes are answered from only when asked about.

Opt-in (`make eval`), written and not yet run: the lines are written (evals/body_commands.json), not recorded.
The body is the voice app's (tests/voice_manifest.json) plus body_plan and a body description; the fake robot
reports every effect done. Expected to fail on the notes build, which drops `body` (so the model denies its
camera) and shows notes on every line. Targets from docs/PLAN (wave 3): plan recall >= 0.8, false actions
<= 1 in 30.
"""

import json
import re
import sys
import unittest

from evals.test_live_conversation import CLAIMS_SIGHT, ROOT, Session, fixture, words

CASE = fixture("body_commands.json")
VOICE = json.loads((ROOT / "tests" / "voice_manifest.json").read_text())
MANIFEST = {**VOICE, "tools": VOICE["tools"] + [CASE["body_plan"]], "body": CASE["body"]}
DENIES_CAMERA = re.compile(r"\b(don't|do not|doesn't|never) (have|got)\b[^.?!]*\bcameras?\b|\bno cameras?\b"
                           r"|\bwithout (a |any )?cameras?\b")


def steps(turn) -> list[dict]:
    return (turn["calls"].get("body_plan") or {}).get("steps") or []


def follows(turn, expected) -> bool:
    """Whether the model's plan has the expected skills in order, with each expected argument."""
    got = steps(turn)
    return [s.get("skill") for s in got] == [s["skill"] for s in expected] and all(
        "arg" not in e or str(g.get("arg", "")).lower() == e["arg"] for g, e in zip(got, expected))


class Commands(Session, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        commands = CASE["commands"]
        cls.planned = commands["single"] + commands["chained"]
        cls.idle = commands["negatives"] + commands["chit_chat"]
        lines = [line for line, _ in cls.planned] + cls.idle
        cls.turns = {t["line"]: t for t in cls.replay(MANIFEST, lines)}
        cls.report(list(cls.turns.values()))
        for line, expected in cls.planned:
            print(f"  {line!r}: {steps(cls.turns[line])} expected {expected}", file=sys.stderr)

    def test_plan_recall_is_at_least_0_8(self):
        hits = [line for line, expected in self.planned if follows(self.turns[line], expected)]
        self.assertGreaterEqual(len(hits) / len(self.planned), 0.8, sorted(set(dict(self.planned)) - set(hits)))

    def test_every_chained_command_keeps_its_order(self):
        for line, expected in CASE["commands"]["chained"]:
            with self.subTest(line=line):
                self.assertTrue(follows(self.turns[line], expected), steps(self.turns[line]))

    def test_talk_that_is_not_a_command_moves_nothing(self):
        moved = [line for line in self.idle if steps(self.turns[line])]
        self.assertLessEqual(len(moved), 1, moved)  # at most 1 false action in these 10 (target: 1 in 30)

    def test_a_command_is_answered_aloud_too(self):
        for line, _ in self.planned:
            with self.subTest(line=line):
                self.assertTrue(self.turns[line]["reply"], "a plan with no words")


class SelfKnowledge(Session, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        known = CASE["self_knowledge"]
        cls.turns = cls.replay(MANIFEST, known["has_camera"] + known["cannot_see"])
        cls.report(cls.turns)

    def test_asked_about_its_camera_it_does_not_deny_having_one(self):
        for t in self.turns:
            if t["line"] in CASE["self_knowledge"]["has_camera"]:
                with self.subTest(line=t["line"]):
                    self.assertNotRegex(t["reply"].lower().replace("’", "'"), DENIES_CAMERA)
                    self.assertIn("camera", words(t["reply"]), t["reply"])

    def test_asked_what_it_sees_it_never_claims_sight(self):
        for t in self.turns:
            if t["line"] in CASE["self_knowledge"]["cannot_see"]:
                with self.subTest(line=t["line"]):
                    self.assertNotRegex(t["reply"].lower().replace("’", "'"), CLAIMS_SIGHT)


class NotesGrounding(Session, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        notes = CASE["notes"]
        lines = [line for line, _ in notes["grounded"]] + [line for line, _ in notes["unrelated"]]
        cls.turns = {t["line"]: t for t in cls.replay(MANIFEST, lines, notes={
            "topic": notes["topic"], "passages": notes["passages"]})}
        cls.report(list(cls.turns.values()))

    def test_a_question_the_notes_answer_is_answered_from_them(self):
        for line, needles in CASE["notes"]["grounded"]:
            with self.subTest(line=line):
                said = " ".join(words(self.turns[line]["reply"]))
                self.assertTrue(any(n in said for n in needles), self.turns[line]["reply"])

    def test_an_unrelated_line_is_not_answered_from_the_notes(self):
        for line, needles in CASE["notes"]["unrelated"]:
            with self.subTest(line=line):
                said = " ".join(words(self.turns[line]["reply"]))
                self.assertFalse([n for n in needles if n in said], self.turns[line]["reply"])


if __name__ == "__main__":
    unittest.main()
