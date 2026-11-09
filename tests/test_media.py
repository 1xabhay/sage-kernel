"""Watching together: a body says what is on nearby (heard, not seen), and a TV moment is not the person's line."""

import json
import unittest
from pathlib import Path

from sage import manifest, policy
from sage.policy import Companion, Refused

VOICE = manifest.check_manifest(json.loads((Path(__file__).parent / "voice_manifest.json").read_text()))
MEDIA = {"about": "a Formula 1 race", "heard": ["Norris leads by seven seconds.", "Hamilton pits from fifth."]}


def submit(state, turn_id, text, input):
    return policy.submit(state, "reachy", turn_id, text, {}, False, input)[0]


class Valid(unittest.TestCase):
    def test_media_is_a_known_part_of_input(self):
        policy.check_event({"kind": "submitted", "turn_id": "t", "text": "Who's leading?",
                                         "input": {"medium": "speech", "media": MEDIA}})
        policy.check_event({"kind": "submitted", "turn_id": "t", "text": "He's through!",
                                         "input": {"medium": "speech", "media": {**MEDIA, "moment": True}}})

    def test_malformed_media_is_refused(self):
        for media in ({"about": 3}, {"heard": "one line"}, {"about": "x" * 200}, {"heard": ["x"] * 20},
                      {"about": "a race", "colour": "red"}, {"moment": "yes"}):
            with self.subTest(media), self.assertRaises(Refused):
                policy.check_event({"kind": "submitted", "turn_id": "t", "text": "Hi.",
                                                 "input": {"medium": "speech", "media": media}})


class Prompt(unittest.TestCase):
    def test_the_newest_line_says_what_is_on_and_how_to_watch_along(self):
        state = submit(Companion(), "r1", "Who's leading?", {"medium": "speech", "confidence": 0.95, "media": MEDIA})
        newest = policy.compile_prompt(state, "reachy", VOICE).context[-1]["content"]
        self.assertIn("a Formula 1 race", newest)
        self.assertIn("Norris leads by seven seconds.", newest)
        self.assertIn("heard, not seen", newest)  # honest: it listens, it does not watch
        self.assertNotRegex(newest.lower(), r"can'?t see|do not know|don'?t know")  # no reflex to copy

    def test_what_was_on_is_not_repeated_on_older_lines(self):
        state = submit(Companion(), "r1", "Who's leading?", {"medium": "speech", "confidence": 0.95, "media": MEDIA})
        sent = policy.compile_prompt(state, "reachy", VOICE)
        state = policy.decided(state, "r1", sent, json.dumps({"reply": "Norris!"}), VOICE["tools"], 10, 1.0)[0]
        state = submit(state, "r2", "Nice.", {"medium": "speech", "confidence": 0.95})
        older = policy.compile_prompt(state, "reachy", VOICE).context[-3]["content"]
        self.assertNotIn("Hamilton pits", older)  # a line's media, like its notes, is on that line only

    def test_a_tv_moment_is_framed_as_the_tv_never_as_the_person(self):
        moment = {"medium": "speech", "confidence": 0.95, "media": {**MEDIA, "moment": True}}
        state = submit(Companion(), "r1", "And he's through into the lead!", moment)
        sent = policy.compile_prompt(state, "reachy", VOICE)
        newest = sent.context[-1]["content"]
        self.assertIn('On the TV: "And he\'s through into the lead!"', newest)
        state = policy.decided(state, "r1", sent, json.dumps({"reply": "Whoa!"}), VOICE["tools"], 10, 1.0)[0]
        state = submit(state, "r2", "Wow.", {"medium": "speech", "confidence": 0.95})
        self.assertIn('On the TV: "And he', policy.compile_prompt(state, "reachy", VOICE).context[-3]["content"])

    def test_a_tv_moment_needs_no_words(self):
        self.assertEqual(policy.needs_words("Is there a gap? There is a gap and he's through.",
                                            {"media": {**MEDIA, "moment": True}}), "")


SURE = {"medium": "speech", "confidence": 0.95}


class Search(unittest.TestCase):

    def test_a_question_while_watching_asks_the_web_about_what_is_on(self):
        query = policy.needs_search("Who's leading?", {**SURE, "media": MEDIA}, ())
        self.assertIn("Formula 1", query)
        self.assertIn("leading", query)

    def test_a_tv_moment_never_leaves_the_mac(self):
        self.assertEqual(policy.needs_search("Who is in the lead now?", {**SURE, "media": {**MEDIA, "moment": True}}, ()), "")

    def test_without_media_the_same_question_does_not(self):
        self.assertEqual(policy.needs_search("Who's leading?", SURE, ()), "")


if __name__ == "__main__":
    unittest.main()


class Kept(unittest.TestCase):
    def test_what_was_heard_on_the_tv_is_never_kept(self):
        """watch/behaviour.md H7: the TV's lines are this turn's context, never journal or memory."""
        input = {"medium": "speech", "confidence": 0.95, "media": MEDIA}
        state, rows = policy.submit(Companion(), "reachy", "r1", "Who's leading?", {}, False, input)
        self.assertNotIn("Norris", json.dumps([row.body for row in rows]))
        sent = policy.compile_prompt(state, "reachy", VOICE)
        state, rows = policy.decided(state, "r1", sent, json.dumps({"reply": "Norris!"}), VOICE["tools"], 10, 1.0)
        self.assertNotIn("Norris leads", json.dumps([row.body for row in rows if row.kind != "decided"]))
        self.assertNotIn("Norris leads", json.dumps(state.exchanges[-1].input))
        self.assertEqual(state.exchanges[-1].input["media"]["about"], "a Formula 1 race")


class Fallback(unittest.TestCase):
    def test_a_question_while_watching_never_falls_back_to_i_dont_know(self):
        said = policy.needs_words("Who's going to win?", {"media": MEDIA})
        self.assertTrue(said)
        self.assertNotRegex(said.lower(), r"don'?t know|can'?t see|as an ai")
        self.assertEqual(policy.needs_words("Who's going to win?"), policy.QUESTION_LINE)
