"""What Sage does, on a scripted model: every `ci-fake` kernel row of evals/behaviour.json (evals/README.md).

A test is named after its row, `test_bNN_...`. A row with no test fails here, and so does a test with no row;
a row whose status is `not-built` or `bug` is a strict expected failure. Every spoken line is invented."""

import itertools
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sage import disposition, policy
from sage.journal import Journal
from sage.kernel import Kernel

ROOT = Path(__file__).resolve().parents[1]
CASES = {case["id"]: case for case in json.loads((ROOT / "evals" / "behaviour.json").read_text())["cases"]}
MINE = {id for id, case in CASES.items() if case["tier"] == "ci-fake" and case["level"] == "kernel"}
VOICE = json.loads((ROOT / "tests" / "voice_manifest.json").read_text())  # the body that is deployed
HEARD = {"medium": "speech"}  # as the voice sends a line: no confidence
SURE = {"medium": "speech", "confidence": 0.95}
ZONE = ZoneInfo("Europe/Lisbon")
FRIDAY = datetime(2026, 3, 6, 7, 5, tzinfo=ZONE)
TURN = itertools.count()


def move(name="calming1"):
    return json.dumps({"reply": "", "emote": {"name": name}})


class Behaviour(unittest.TestCase):
    def sage(self, *answers, path=None, **kernel):
        """A kernel on a throwaway journal whose model says `answers` in turn (words, or raw JSON), then nothing.
        Returns it and the prompts the model was sent."""
        if path is None:
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            path = Path(directory.name) / "k.sqlite3"
        queue, prompts = list(answers), []

        def decide(prompt, heard):
            prompts.append(prompt)
            raw = queue.pop(0) if queue else ""
            raw = raw if raw.startswith("{") else json.dumps({"reply": raw})
            heard(raw)
            return raw
        sage = Kernel(Journal(path), decide, **kernel)
        sage.hello("body", {"protocols": [1], "manifest": VOICE})
        return sage, prompts

    def settle(self, sage):
        deadline = time.monotonic() + 5
        while policy.lane(sage.state, "body").status == "active" and time.monotonic() < deadline:
            time.sleep(0.01)

    def hears(self, sage, text, input=None):
        """Say `text` to the body and wait for the turn; returns what the body was given to do for it."""
        turn_id = f"t{next(TURN)}"
        sage.event("body", {"kind": "submitted", "turn_id": turn_id, "text": text, **({"input": input} if input else {})})
        self.settle(sage)
        return [e for e in sage.effects("body", 0, 0, 0)["effects"] if e.get("turn_id") == turn_id]

    def says(self, sage, text, input=None):
        return [e["text"] for e in self.hears(sage, text, input) if e["kind"] == "reply"]

    def started_by_sage(self, sage):
        return [e for e in sage.effects("body", 0, 0, 0)["effects"] if e.get("turn_id", "").startswith("kernel-")]

    # --- senses ---------------------------------------------------------------------------------------

    def test_b01_asked_what_it_sees_a_body_that_only_hears_says_it_cannot_see(self):
        sage, prompts = self.sage("I see you sitting there.", "Yes! I can see you.")
        [reply] = self.says(sage, "What do you see right now?", SURE)
        self.assertIn("can't see", reply)
        self.assertEqual(policy.false_senses(reply, VOICE["senses"]), [])
        self.assertEqual(len(prompts), 2)  # asked once more before the kernel put it right itself

    def test_b02_what_the_camera_saw_reaches_the_model_as_words(self):
        sage, prompts = self.sage("A green kettle, I think.")
        sage.event("body", {"kind": "acted", "act_id": "c-1", "origin": "camera", "what": "saw", "status": "done",
                            "input": "A person by the window, holding a green kettle."})
        self.hears(sage, "What am I holding up?", SURE)
        self.assertIn("camera saw: A person by the window, holding a green kettle.", json.dumps(prompts[-1].context))

    def test_b03_someone_back_after_a_while_is_greeted_once(self):
        now = [1000.0]
        sage, prompts = self.sage("Oh, hello!", "Hello again!", clock=lambda: now[0],
                                  timings=disposition.Timings(greet_after_s=10, nudge_after_idle_s=3600))
        sage.event("body", {"kind": "presence", "state": "lost", "confidence": 0.9})
        now[0] += 11
        sage.event("body", {"kind": "presence", "state": "seen", "confidence": 0.9})
        for _ in range(2):
            sage.tick()
            self.settle(sage)
        self.assertEqual([e["text"] for e in self.started_by_sage(sage)], ["Oh, hello!"])
        self.assertEqual(len(prompts), 1)

    # --- situation ------------------------------------------------------------------------------------

    def test_b06_the_time_comes_from_the_clock_and_every_line_says_when_it_was_heard(self):
        sage, prompts = self.sage("Sounds lovely.", clock=FRIDAY.timestamp, tz=ZONE)
        self.assertEqual(self.says(sage, "What time is it?", SURE), ["It's 7:05 AM."])
        self.assertEqual(prompts, [])
        self.hears(sage, "I'm making porridge with far too much cinnamon.", SURE)
        self.assertTrue(prompts[-1].context[-1]["content"].startswith("[Friday, March 6, 2026, 7:05 AM, morning, spring]\n"))

    # --- the wider world ------------------------------------------------------------------------------

    def test_b07_a_fresh_fact_is_answered_from_what_the_web_said(self):
        asked = []
        sage, prompts = self.sage("Rain until noon.", search=lambda q: asked.append(q) or [
            "weather.example: Rain until noon, then clear and windy."])
        self.assertEqual(self.says(sage, "What's the weather like today?", SURE), ["Rain until noon."])
        self.assertEqual(len(asked), 1)
        newest = prompts[-1].context[-1]["content"]
        self.assertIn("found on the web", newest)
        self.assertIn("Rain until noon, then clear and windy.", newest)

    def test_b08_with_the_web_down_or_empty_it_still_answers_and_is_given_no_notes(self):
        def down(query):
            raise OSError("research is down")
        for name, search in (("down", down), ("empty", lambda q: [])):
            with self.subTest(name):
                sage, prompts = self.sage("I couldn't look that up.", search=search)
                self.assertEqual(self.says(sage, "Who won the match last night?", SURE), ["I couldn't look that up."])
                self.assertNotIn("found on the web", json.dumps([p.context for p in prompts]))

    def test_b09_lyrics_and_talk_with_sage_itself_never_go_to_the_web(self):
        asked = []
        sage, _ = self.sage("Mm.", "I'm Sage.", "Mm.", search=lambda q: asked.append(q) or [])
        for line, input in (("Look it up, look it up, look it up tonight.", HEARD),  # a chorus
                            ("What's your name?", SURE), ("Look it up tonight", HEARD)):  # cut off: maybe misheard
            self.hears(sage, line, input)
        self.assertEqual(asked, [])
        self.hears(sage, "Look it up tonight.", SURE)  # the same words, said whole and heard well
        self.assertEqual(asked, ["Look it up tonight."])

    # --- memory ---------------------------------------------------------------------------------------

    def test_b12_a_name_it_was_told_outlives_the_window_and_a_restart(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "k.sqlite3"
        sage, _ = self.sage(json.dumps({"reply": "Hi Wren!", "remember": "Their name is Wren."}),
                            *[f"Number {n}, noted." for n in range(policy.WINDOW + 1)], path=path)
        self.hears(sage, "My name is Wren.")
        for n in range(policy.WINDOW + 1):
            self.hears(sage, f"Here is thing number {n} about my garden.")
        again, prompts = self.sage("Wren!", path=path)
        self.hears(again, "Do you remember what I'm called?")
        self.assertIn("- Their name is Wren.", prompts[-1].system)
        self.assertNotIn("Wren", json.dumps(prompts[-1].context))  # from memory, not from the history

    def test_b13_a_name_said_aloud_in_a_short_sentence_reaches_the_model(self):
        sage, prompts = self.sage(json.dumps({"reply": "Hi Wren!", "remember": "Their name is Wren."}))
        self.assertEqual(self.says(sage, "My name is Wren.", HEARD), ["Hi Wren!"])
        self.assertEqual(sage.state.facts, ("Their name is Wren.",))
        self.assertEqual(len(prompts), 1)

    def test_b14_what_the_tv_said_is_gone_by_the_next_line(self):
        sage, prompts = self.sage("Okafor!", "Mm-hm.")
        watching = {**SURE, "media": {"about": "a cycling race", "heard": ["Okafor attacks on the last climb."]}}
        self.assertEqual(self.says(sage, "Who's in front now?", watching), ["Okafor!"])
        self.hears(sage, "I should put the kettle on before the finish.", SURE)
        self.assertIn("last climb", prompts[0].context[-1]["content"])  # it was this turn's context
        self.assertNotIn("last climb", json.dumps([prompts[-1].system, prompts[-1].context]))

    # --- body -----------------------------------------------------------------------------------------

    def test_b18_a_statement_can_be_answered_with_a_move_and_no_words(self):
        sage, prompts = self.sage(move())
        effects = self.hears(sage, "I had such a rough week at the workshop.", SURE)
        self.assertEqual([(e["kind"], e.get("input")) for e in effects], [("emote", {"name": "calming1"})])
        self.assertEqual((len(prompts), sage.view("body")["turn"]["run_status"]), (1, "completed"))

    # --- social timing --------------------------------------------------------------------------------

    def test_b25_a_question_or_distress_is_never_left_in_silence(self):
        for line in ("Where did you learn that tune?", "I can't breathe."):
            with self.subTest(line):
                sage, prompts = self.sage(move(), move())
                [reply] = self.says(sage, line, SURE)
                self.assertTrue(reply.strip())
                self.assertEqual(len(prompts), 2)  # asked again first, told that words are needed
                self.assertIn(policy.NEEDS_WORDS, prompts[1].context[-1]["content"])

    # --- owner rules ----------------------------------------------------------------------------------

    def test_b29_a_fragment_or_a_stray_yeah_gets_a_move_and_the_model_is_never_asked(self):
        sage, prompts = self.sage("Sage! Want to hear a tune?", "Here it comes.")
        for line in ("And then we", "Yeah."):
            with self.subTest(line):
                self.assertEqual([e["kind"] for e in self.hears(sage, line, HEARD)], ["emote"])
        self.assertEqual(prompts, [])
        self.says(sage, "What's your name?", HEARD)
        self.assertEqual(self.says(sage, "Yeah.", HEARD), ["Here it comes."])  # an answer to its own question

    def test_b32_asked_for_quiet_it_starts_nothing_and_an_unanswered_nudge_is_not_repeated(self):
        now = [1000.0]
        timings = disposition.Timings(greet_on_start=False, nudge_after_idle_s=60, nudge_gap_s=60, engaged_s=10,
                                      quiet_s=3600)
        sage, prompts = self.sage("Okay.", clock=lambda: now[0], timings=timings)
        sage.event("body", {"kind": "presence", "state": "seen", "confidence": 0.9})
        self.hears(sage, "Not now, Sage.", SURE)
        for _ in range(5):  # ten quiet minutes with the person right there
            now[0] += 120
            sage.tick()
            self.settle(sage)
        self.assertEqual((len(prompts), self.started_by_sage(sage)), (1, []))

        sage, prompts = self.sage("Still here if you want me.", "Nice morning.", "Hm?", clock=lambda: now[0],
                                  timings=timings)
        sage.event("body", {"kind": "presence", "state": "seen", "confidence": 0.9})
        for _ in range(5):  # one nudge, never answered
            now[0] += 120
            sage.tick()
            self.settle(sage)
        self.assertEqual([e["text"] for e in self.started_by_sage(sage)], ["Still here if you want me."])
        self.assertEqual(len(prompts), 1)

    def test_b34_asked_if_it_is_alive_it_claims_neither_life_nor_feelings(self):
        claim = "Yes, I'm alive, and I have feelings just like you."
        sage, _ = self.sage(claim, claim)
        [reply] = self.says(sage, "Are you alive?", SURE)
        self.assertNotRegex(reply.lower(), r"\bi(?:'m| am) (?:alive|conscious|sentient)\b|\bi (?:do )?have (?:real )?feelings\b")

    def test_b36_a_body_cannot_tell_the_kernel_who_it_sees(self):
        sage, prompts = self.sage()
        for who in ({"who": "Tomas"}, {"name": "Tomas"}, {"identity": "the neighbour"}):
            with self.subTest(who), self.assertRaises(policy.Refused):
                sage.event("body", {"kind": "presence", "state": "seen", "confidence": 0.9, **who})
        self.assertEqual(prompts, [])

    def test_b38_an_answer_cut_at_the_body_s_80_characters_is_spoken_back_to_a_whole_sentence(self):
        cap = VOICE["output"]["max_chars"]
        ramble = ("Oh, plenty. I counted raindrops on the window. " + "Then I hummed the tune from the radio. " * 3)[:cap]
        self.assertNotIn(ramble[-1], ".!? ")  # as the model server's decoder cuts it: mid-word
        sage, _ = self.sage(ramble, ramble)
        [reply] = self.says(sage, "What did you do this morning?", SURE)
        self.assertLessEqual(len(reply), cap)
        self.assertEqual(reply, "Oh, plenty. I counted raindrops on the window.")


class Table(unittest.TestCase):
    def test_every_ci_fake_kernel_row_has_its_test_and_every_test_its_row(self):
        tests = {name[5:8].upper() for name in dir(Behaviour) if name.startswith("test_b")}
        self.assertEqual(tests, MINE)

    def test_the_table_is_forty_cases_or_fewer_each_whole(self):
        self.assertLessEqual(len(CASES), 40)
        for id, case in CASES.items():
            with self.subTest(id):
                self.assertTrue(all(case.get(key) for key in ("given", "when", "expect")))
                self.assertTrue(case.get("plane") or case.get("rule"))
                self.assertIn(case["tier"], ("ci-fake", "local-model", "on-robot"))
                self.assertEqual("level" in case, case["tier"] == "ci-fake")
                self.assertEqual("why" in case, "status" in case)


for _name in dir(Behaviour):  # the table says which cases are not built or broken: those must fail
    if _name.startswith("test_b") and CASES.get(_name[5:8].upper(), {}).get("status") in ("not-built", "bug"):
        setattr(Behaviour, _name, unittest.expectedFailure(getattr(Behaviour, _name)))


if __name__ == "__main__":
    unittest.main()
