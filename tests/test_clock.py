"""Sage knows the time: each line carries when it was heard, and exact time questions are never guessed."""

import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sage import manifest, policy
from sage.journal import Journal
from sage.kernel import Kernel
from sage.policy import Companion

RAW = json.loads((Path(__file__).parent / "voice_manifest.json").read_text())
VOICE = manifest.check_manifest(RAW)
EAST = ZoneInfo("America/New_York")
SUNDAY = datetime(2026, 11, 8, 12, 41, tzinfo=EAST)


class Situation(unittest.TestCase):
    def test_the_line_names_the_weekday_date_time_day_part_and_season(self):
        self.assertEqual(policy.situation(SUNDAY), "Sunday, November 8, 2026, 12:41 PM, afternoon, autumn")
        self.assertEqual(policy.situation(SUNDAY, "Springfield"),
                         "Sunday, November 8, 2026, 12:41 PM, afternoon, autumn, in Springfield")

    def test_day_parts_and_seasons(self):
        part = {h: policy.situation(SUNDAY.replace(hour=h)).split(", ")[4] for h in (4, 5, 11, 12, 16, 17, 20, 21, 0)}
        self.assertEqual(part, {4: "night", 5: "morning", 11: "morning", 12: "afternoon", 16: "afternoon",
                                17: "evening", 20: "evening", 21: "night", 0: "night"})
        seasons = [policy.situation(SUNDAY.replace(month=m, day=1)).split(", ")[-1] for m in (1, 3, 6, 9, 12)]
        self.assertEqual(seasons, ["winter", "spring", "summer", "autumn", "winter"])


class Prompt(unittest.TestCase):
    def submit(self, state, turn_id, text, when):
        return policy.submit(state, "reachy", turn_id, text, {}, False, None, when=when)[0]

    def test_each_line_says_when_it_was_heard_and_the_cached_prefix_still_holds(self):
        state = self.submit(Companion(), "r1", "Hi.", "Sunday, November 8, 2026, 12:41 PM, afternoon, autumn")
        sent = policy.compile_prompt(state, "reachy", VOICE)
        self.assertTrue(sent.context[-1]["content"].startswith(
            "[Sunday, November 8, 2026, 12:41 PM, afternoon, autumn]\nHi."))
        output = json.dumps({"reply": "Hi!"})
        state = policy.decided(state, "r1", sent, output, VOICE["tools"], 10, 1.0)[0]
        state = self.submit(state, "r2", "And now?", "Sunday, November 8, 2026, 12:43 PM, afternoon, autumn")
        expected = sent.messages() + [{"role": "assistant", "content": output}]
        later = policy.compile_prompt(state, "reachy", VOICE)
        self.assertEqual(later.messages()[:len(expected)], expected)  # byte for byte: the model server's cache
        self.assertIn("12:43 PM", later.context[-1]["content"])

    def test_without_a_time_the_prompt_is_as_before(self):
        state = self.submit(Companion(), "r1", "Hi.", "")
        self.assertEqual(policy.compile_prompt(state, "reachy", VOICE).context[-1]["content"].split("\n")[0], "Hi.")

    def test_a_proactive_turn_carries_the_time_too(self):
        state = policy.start_proactive(Companion(), "reachy", "k1", "greet", when="Sunday, 8 AM")[0]
        self.assertTrue(policy.compile_prompt(state, "reachy", VOICE).context[-1]["content"].startswith("[Sunday, 8 AM]\n"))


class ExactAnswers(unittest.TestCase):
    def test_time_date_and_day_questions_are_answered_from_the_clock(self):
        cases = {"What time is it?": "It's 12:41 PM.", "what's the time": "It's 12:41 PM.",
                 "Do you know what time it is?": "It's 12:41 PM.", "What's the date today?": "It's Sunday, November 8.",
                 "What day is it?": "It's Sunday.", "what day is today": "It's Sunday."}
        self.assertEqual({line: policy.clock_answer(line, SUNDAY) for line in cases}, cases)

    def test_other_lines_and_other_places_are_left_to_the_model(self):
        for line in ("I had a great time today.", "What time is it in Tokyo?", "What a day!", "Time for bed.",
                     "What's the date of the concert?"):
            self.assertEqual(policy.clock_answer(line, SUNDAY), "", line)

    def test_every_exact_answer_fits_the_voice_cap(self):
        late = datetime(2026, 9, 30, 23, 59, tzinfo=EAST)  # Wednesday, September 30: the longest day and month
        for line in ("What time is it?", "What's the date?", "What day is it?"):
            self.assertLessEqual(len(policy.clock_answer(line, late)), 60)


class KernelClock(unittest.TestCase):
    def test_the_kernel_answers_the_time_without_the_model_in_its_own_zone(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        prompts = []

        def decide(prompt, heard):
            prompts.append(prompt)
            return json.dumps({"reply": "Hello!"})
        kernel = Kernel(Journal(Path(directory.name) / "k.sqlite3"), decide, clock=lambda: SUNDAY.timestamp(), tz=EAST)
        kernel.hello("reachy", {"protocols": [1], "manifest": RAW})
        kernel.event("reachy", {"kind": "submitted", "turn_id": "t1", "text": "What time is it?", "timing": {}})
        deadline = time.monotonic() + 5
        while kernel.state.turn.status == "active" and time.monotonic() < deadline:
            time.sleep(0.01)
        replies = [e["text"] for e in kernel.effects("reachy", 0, 0, 0)["effects"] if e["kind"] == "reply"]
        self.assertEqual(replies, ["It's 12:41 PM."])
        self.assertEqual([p for p in prompts if "time is it" in p.context[-1]["content"]], [])
        kernel.event("reachy", {"kind": "submitted", "turn_id": "t2", "text": "Nice.", "timing": {}})
        deadline = time.monotonic() + 5
        while kernel.state.turn.status == "active" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(prompts[-1].context[-1]["content"].startswith(
            "[Sunday, November 8, 2026, 12:41 PM, afternoon, autumn]\n"))


class QuietMoves(unittest.TestCase):
    """Replay, 11-09 evening: a misheard fragment or a bare "Yeah." no one said to Sage got words from the 4B
    model ("Tomorrow? Let's see what happens."), and those words became what it then "remembered". A move only,
    decided by the kernel, never by the model."""
    TOOLS = ("emote",)

    def test_fragments_and_unsure_lines_get_a_move_and_no_words(self):
        for line in ("How", "How do you", "And", "How about", "I was gonna.", "Rain keeping cage."):
            with self.subTest(line):
                out = json.loads(policy.quiet_move(line, {"medium": "speech"}, ("sounds good!",), self.TOOLS))
                self.assertEqual(out["reply"], "")

    def test_a_bare_yeah_or_no_gets_words_only_when_sage_just_asked(self):
        for line in ("Yeah.", "No.", "Mm-hmm.", "Yeah, yeah, yeah.", "Hmm."):
            with self.subTest(line):
                self.assertTrue(policy.quiet_move(line, {"medium": "speech"}, ("sounds good!",), self.TOOLS))
                self.assertEqual(policy.quiet_move(line, {"medium": "speech"}, ("want to hear a song?",), self.TOOLS), "")

    def test_questions_distress_typed_lines_and_whole_sentences_go_to_the_model(self):
        for line, input in (("What's going on?", {"medium": "speech"}), ("Help me!", {"medium": "speech"}),
                            ("ok", {"medium": "text"}), ("That's what I'm going to do.", {"medium": "speech"}),
                            ("Call me Micah", {"medium": "speech", "confidence": 0.3,  # PROTOCOL: the model picks
                                               "alternatives": ["Call me Mika"]}),
                            ("[The person is back.]", {})):
            with self.subTest(line):
                self.assertEqual(policy.quiet_move(line, input, (), self.TOOLS), "")

    def test_a_short_whole_statement_said_to_sage_goes_to_the_model(self):
        """ "My name's Lena." is the persona's own example of a name to remember. The voice sends no confidence,
        so a short line is the model's when it is whole and said to someone: it names Sage, says I or you, or
        starts as a request."""
        for line in ("My name's Lena.", "Call me Mika.", "I'm sad.", "I love you.", "Look at me.", "Sing a song.",
                     "Good night, Sage.", "Sage, count for me.", "Wake up."):
            with self.subTest(line):
                self.assertEqual(policy.quiet_move(line, {"medium": "speech"}, (), self.TOOLS), "")

    def test_a_short_line_said_to_no_one_or_left_hanging_still_gets_a_move(self):
        for line in ("Only the kettle.", "Come on.", "I'm just gonna.", "You and the.", "Nice day today."):
            with self.subTest(line):
                self.assertTrue(policy.quiet_move(line, {"medium": "speech"}, (), self.TOOLS))

    def test_the_quiet_move_is_one_the_body_declared(self):
        """A body whose emote has no "attentive1" has the kernel's own move refused, and the model is then told
        its body "could not do emote: invalid input"."""
        emote = {"name": "emote", "inputSchema": {"type": "object", "properties": {"name": {"enum": ["nod1"]}}}}
        declared = manifest.check_manifest({"senses": ["speech"], "tools": [emote]})
        state = policy.submit(Companion(), "body", "t1", "How do you", {}, False, {"medium": "speech"})[0]
        quiet = policy.quiet_move("How do you", {"medium": "speech"}, (), ("emote",), policy.clips(declared["tools"]))
        state, rows = policy.decided(state, "t1", policy.compile_prompt(state, "body", declared), quiet,
                                     declared["tools"], 10, 1.0)
        self.assertEqual([call.outcome for call in state.exchanges[-1].calls], [""])  # nothing it could not do
        self.assertEqual([row.kind for row in rows], ["decided", "emote", "completed"])

    def test_a_body_without_emote_gets_silence_alone(self):
        self.assertEqual(json.loads(policy.quiet_move("How do you", {"medium": "speech"}, (), ())), {"reply": ""})

    def test_the_kernel_never_asks_the_model_for_a_fragment(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        prompts = []

        def decide(prompt, heard):
            prompts.append(prompt)
            return json.dumps({"reply": "Tomorrow? Let's see what happens."})
        kernel = Kernel(Journal(Path(directory.name) / "k.sqlite3"), decide, clock=lambda: SUNDAY.timestamp(), tz=EAST)
        kernel.hello("reachy", {"protocols": [1], "manifest": RAW})
        kernel.event("reachy", {"kind": "submitted", "turn_id": "t1", "text": "How do you", "timing": {},
                                "input": {"medium": "speech"}})
        deadline = time.monotonic() + 5
        while kernel.state.turn.status == "active" and time.monotonic() < deadline:
            time.sleep(0.01)
        replies = [e["text"] for e in kernel.effects("reachy", 0, 0, 0)["effects"] if e["kind"] == "reply"]
        self.assertEqual((prompts, [r for r in replies if r]), ([], []))


if __name__ == "__main__":
    unittest.main()
