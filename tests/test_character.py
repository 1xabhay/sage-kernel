"""The pet's character is a file (sage/character.json, or the one SAGE_CHARACTER names), not the kernel's code.

Each case starts a new Python: the file is read once, when the kernel starts."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "sage" / "character.json"
# What a kernel with this character sends and says, as JSON on stdout.
SHOW = """
import json
from sage import manifest, policy, speech
body = manifest.check_manifest({"senses": ["speech"], "tools": []})
state = policy.submit(policy.Companion(), "body", "t1", "Tell me about your day.", {}, False)[0]
prompt = policy.compile_prompt(state, "body", body)
print(json.dumps({"system": prompt.system, "line": prompt.context[-1]["content"],
                  "distress": policy.needs_words("Help me!"), "question": policy.needs_words("Why is it dark?"),
                  "alive": policy.honest("I am alive.", ("speech",)),
                  "clock": policy.clock_answer("What day is it?", __import__("datetime").datetime(2026, 1, 5, 9, 30)),
                  "copied": speech.speakable("Hi. [Stay Momo: short.]", (), True)}))
"""

# How a kernel with this character reads what it hears.
READS = """
import json
from datetime import datetime
from sage import disposition, policy, speech
monday = datetime(2026, 1, 5, 21, 30)
print(json.dumps({"distress": [policy.needs_words("Hilfe!"), policy.needs_words("Help me!")],
                  "question": policy.needs_words("Warum ist es dunkel."),
                  "quiet": [disposition.asks_for_quiet("Ruhe bitte."), disposition.asks_for_quiet("Be quiet.")],
                  "day": policy.clock_answer("Welcher Tag ist heute?", monday), "when": policy.situation(monday),
                  "misheard": [policy.misheard("Hallo"), policy.misheard("Hello"), policy.misheard("Tanz mal.")],
                  "repeat": [policy.asks_to_repeat("Wie bitte?"), policy.asks_to_repeat("Say that again.")],
                  "alive": policy.claims_life("Ich bin lebendig."),
                  "sees": policy.false_senses("Ich sehe dich.", ("speech",)),
                  "interrupted": speech.speakable("Du hast mich unterbrochen. Gut.", (), True),
                  "terms": policy._terms("der Hund und die Katzen")}))
"""


def python(*args: str, character: Path | None = None) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != "SAGE_CHARACTER"}
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, timeout=60, check=False,
                          env={**env, **({"SAGE_CHARACTER": str(character)} if character else {})})


class CharacterFile(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.file = Path(folder.name) / "momo.json"
        self.character = json.loads(DEFAULT.read_text(encoding="utf-8"))

    def write(self) -> Path:
        self.file.write_text(json.dumps(self.character), encoding="utf-8")
        return self.file

    def refused(self) -> str:
        """What the kernel says as it refuses to start on the file."""
        started = python("-m", "sage", character=self.write())
        self.assertNotEqual(started.returncode, 0)
        self.assertIn(str(self.file), started.stderr)
        self.assertNotIn("Traceback", started.stderr)
        return started.stderr

    def test_another_file_is_another_pet(self):
        momo = self.character
        momo["name"], momo["never_said"] = "Momo", ["stay momo"]
        momo["prompt"].update(persona="You are Momo, a cat.", stay="[Stay Momo: short.]", examples=["Hi. -> {\"reply\": \"Mrow.\"}"])
        momo["lines"].update(distress="Momo is here.", question="Momo does not know.", alive="Momo is a toy.")
        momo["templates"]["clock_day"] = "Today is {day}."
        shown = python("-c", SHOW, character=self.write())
        self.assertEqual(shown.returncode, 0, shown.stderr)
        got = json.loads(shown.stdout)
        self.assertTrue(got["system"].startswith('You are Momo, a cat.\n\nExamples of how you answer:\nHi. -> {"reply": "Mrow."}\n\n[Body'))
        self.assertNotIn("Sage", got["system"] + got["line"])
        self.assertIn("\n\n[Stay Momo: short.]", got["line"])
        self.assertEqual(got["distress"], "Momo is here.")
        self.assertEqual(got["question"], "Momo does not know.")
        self.assertEqual(got["alive"], "Momo is a toy.")
        self.assertEqual(got["clock"], "Today is Monday.")
        self.assertEqual(got["copied"], "Hi.")

    def test_with_no_file_named_the_pet_is_sage(self):
        shown = python("-c", SHOW)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        got = json.loads(shown.stdout)
        self.assertTrue(got["system"].startswith("You are Sage, a small robot companion"))

    def test_a_file_missing_a_key_stops_the_start_and_names_it(self):
        del self.character["prompt"]["persona"]
        self.assertIn("prompt.persona", self.refused())

    def test_a_key_of_the_wrong_kind_stops_the_start_and_names_it(self):
        self.character["lines"]["distress"] = 5
        self.assertIn("lines.distress", self.refused())

    def test_a_note_with_a_field_the_kernel_does_not_fill_stops_the_start(self):
        self.character["templates"]["notes"] = "[Notes on {subject}:]"
        self.assertIn("templates.notes", self.refused())

    def test_another_file_is_another_language(self):
        """The word lists and patterns the kernel reads English with are the file's too."""
        german = self.character
        german["calendar"].update(days=["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
                                  day_parts=["Morgen", "Nachmittag", "Abend", "Nacht"])
        german["calendar"]["months"][0], german["calendar"]["seasons"][0] = "Januar", "Winter"
        german["patterns"].update(distress=[r"\bhilfe\b"], question=["(?:warum|wer|was|wo)\\b"], quiet=[r"\bruhe\b"],
                                  asks_day=["^welcher tag ist heute\\b"], asks_to_repeat=["^wie bitte"],
                                  alive=[r"\bich bin lebendig\b"], interruption=[r"\bunterbrochen\b"],
                                  suffix=["(en|e|n)$"])
        german["patterns"]["claims"]["vision"] = [r"\bich sehe dich\b"]
        german["words"].update(common=["hallo"], stopwords=["der", "die", "das", "und"])
        shown = python("-c", READS, character=self.write())
        self.assertEqual(shown.returncode, 0, shown.stderr)
        got = json.loads(shown.stdout)
        self.assertEqual(got["distress"], [german["lines"]["distress"], ""])
        self.assertEqual(got["question"], german["lines"]["question"])
        self.assertEqual(got["quiet"], [True, False])
        self.assertEqual(got["day"], "It's Montag.")
        self.assertEqual(got["when"], "Montag, Januar 5, 2026, 9:30 PM, Nacht, Winter")
        self.assertEqual(got["misheard"], [False, True, False])
        self.assertEqual(got["repeat"], [True, False])
        self.assertTrue(got["alive"])
        self.assertEqual(got["sees"], ["vision"])
        self.assertEqual(got["interrupted"], "Gut.")
        self.assertEqual(got["terms"], ["hund", "katz"])

    def test_a_pattern_that_is_no_regular_expression_stops_the_start_and_names_it(self):
        self.character["patterns"]["distress"] = ["help(", "fire"]
        self.assertIn("patterns.distress", self.refused())

    def test_a_pattern_that_would_match_every_line_stops_the_start(self):
        """No alternatives, or an empty one, is a pattern every line matches: every line would be distress."""
        self.character["patterns"]["claims"]["touch"] = []
        self.assertIn("patterns.claims.touch", self.refused())
        self.character["patterns"]["claims"]["touch"] = ["x", ""]
        self.assertIn("patterns.claims.touch", self.refused())

    def test_a_year_of_eleven_months_stops_the_start(self):
        del self.character["calendar"]["months"][-1]
        self.assertIn("calendar.months", self.refused())

    def test_a_file_that_is_not_json_or_not_there_stops_the_start(self):
        self.file.write_text("{", encoding="utf-8")
        started = python("-m", "sage", character=self.file)
        self.assertNotEqual(started.returncode, 0)
        self.assertIn(str(self.file), started.stderr)
        missing = python("-m", "sage", character=self.file.with_name("none.json"))
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("none.json", missing.stderr)


if __name__ == "__main__":
    unittest.main()
