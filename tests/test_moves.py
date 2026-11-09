"""A turn that ends with a move and no words, and the lines where silence is never allowed, on any body
(docs/PROTOCOL.md "Moves")."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from sage import manifest, policy
from sage.journal import Journal
from sage.kernel import Kernel
from sage.policy import Companion

SOOTHE, AGREE, UNSURE = {"wave": {"how": "slow"}}, {"wave": {"how": "nod"}}, {"wave": {"how": "tilt"}}
WAVE = {"name": "wave", "description": "Move.", "max_s": 3,
        "inputSchema": {"type": "object", "properties": {"how": {"enum": ["slow", "nod", "tilt"]}}, "required": ["how"]}}
MOVES = {"comfort": SOOTHE, "agree": AGREE, "unsure": UNSURE}
MOVING = {"senses": ["speech"], "tools": [WAVE], "moves": MOVES}
STILL = {"senses": ["speech"], "tools": []}


def answer(reply, **tools):
    return json.dumps({"reply": reply, **tools})


def prompt_for(declared, text="Hi"):
    state = policy.submit(Companion(), "body", "t1", text, {}, False)[0]
    return policy.compile_prompt(state, "body", manifest.check_manifest(declared))


class Declared(unittest.TestCase):
    def test_a_manifest_keeps_valid_moves(self):
        self.assertEqual(manifest.check_manifest(MOVING)["moves"], MOVES)
        self.assertNotIn("moves", manifest.check_manifest(STILL))

    def test_moves_must_be_answers_this_body_can_perform(self):
        for moves in ({"comfort": {"fly": {}}}, {"comfort": {"wave": {"how": "spin"}}}, {"comfort": {}},
                      {"dance": SOOTHE}, {}, "wave", {"comfort": "wave"}, {"comfort": {"reply": "Hi"}}):
            with self.subTest(moves=moves), self.assertRaises(ValueError):
                manifest.check_manifest({**MOVING, "moves": moves})


class Prompt(unittest.TestCase):
    def test_a_body_with_moves_is_taught_silence_with_its_own_moves(self):
        prompt = prompt_for(MOVING)
        lines = prompt.system.split("\n\n")[1].splitlines()[1:]
        shown = [json.loads(line.split(" -> ", 1)[1]) for line in lines]
        for move in MOVES.values():
            self.assertIn({"reply": "", **move}, shown)
        self.assertTrue(any(example["reply"] for example in shown))  # a direct question still gets words
        self.assertTrue(all(next(iter(example)) == "reply" for example in shown))  # reply first
        self.assertIn(policy.MOVING_STAY, prompt.context[-1]["content"])
        for chatty in ("ask them", "their name", "something back"):
            self.assertNotIn(chatty, policy.MOVING_PERSONA)

    def test_the_kernel_names_no_body_s_parts_or_moves(self):
        for name in ("calming1", "confused1", "emote", "antenna", "reachy"):
            self.assertNotIn(name, (policy.MOVING_PERSONA + policy.MOVING_STAY).lower())

    def test_only_the_roles_a_body_declares_become_examples(self):
        system = prompt_for({**MOVING, "moves": {"unsure": UNSURE}}).system
        self.assertIn('"tilt"', system.split("\n\n")[1])
        self.assertNotIn('"slow"', system.split("\n\n")[1])

    def test_a_body_without_moves_keeps_the_old_prompt(self):
        prompt = prompt_for(STILL)
        self.assertTrue(prompt.system.startswith(policy.PERSONA + "\n\n" + policy.EXAMPLES))
        self.assertIn(policy.STAY, prompt.context[-1]["content"])

    def test_a_text_body_keeps_its_prompt_whatever_it_declares(self):
        prompt = prompt_for({**MOVING, "output": {"medium": "text"}})
        self.assertTrue(prompt.system.startswith(policy.TEXT_PERSONA))
        self.assertIn(policy.STAY, prompt.context[-1]["content"])

    def test_the_prefix_is_byte_stable_across_silent_and_spoken_turns(self):
        declared = manifest.check_manifest(MOVING)
        state, silent, seen = Companion(), answer("", **SOOTHE), []
        for n, (line, raw) in enumerate((("One.", silent), ("Two.", answer("Hm.")), ("Three.", silent))):
            state = policy.submit(state, "body", f"t{n}", line, {}, False)[0]
            prompt = policy.compile_prompt(state, "body", declared)
            seen.append(prompt)
            state = policy.decided(state, f"t{n}", prompt, raw, declared["tools"], 10 + 10 * n, 1.0)[0]
        self.assertEqual({prompt.system for prompt in seen}, {seen[0].system})
        self.assertEqual(seen[2].context[:3], seen[1].context)
        self.assertEqual(seen[2].context[3]["content"], answer("Hm."))
        self.assertEqual(seen[1].context[1]["content"], silent)  # silence in history, as the model answered it


class NeedsWords(unittest.TestCase):
    def test_a_direct_question_needs_words(self):
        for line in ("What's your name?", "whats your name", "who are you", "Do you like rain", "are you there",
                     "I had a long day. Can you hear me", "So you like rain?", "tell me a joke", "How old are you."):
            with self.subTest(line):
                self.assertEqual(policy.needs_words(line), policy.QUESTION_LINE)

    def test_distress_needs_words_however_it_is_heard(self):
        for line in ("I want to die", "i want to kill myself.", "Help me", "help", "I can't breathe", "I cant breathe",
                     "I’m scared", "someone is hurting me", "I think I'm having a heart attack", "Call an ambulance",
                     "I don't want to be here anymore?", "suicide"):
            with self.subTest(line):
                self.assertEqual(policy.needs_words(line), policy.DISTRESS_LINE)

    def test_a_statement_or_a_kernel_note_may_get_silence(self):
        for line in ("I had such a long day.", "I love rainy mornings", "What a day!", "the fridge banana wire",
                     "Hi", "Do a dance", policy.PROACTIVE["nudge"], policy.PROACTIVE["greet"]):
            with self.subTest(line):
                self.assertEqual(policy.needs_words(line), "")


class Decided(unittest.TestCase):
    def decide(self, text, raw):
        declared = manifest.check_manifest(MOVING)
        state = policy.submit(Companion(), "body", "t1", text, {}, False)[0]
        prompt = policy.compile_prompt(state, "body", declared)
        return policy.decided(state, "t1", prompt, raw, declared["tools"], 10, 1.0)

    def test_a_move_with_no_words_is_journaled_as_a_move_and_no_reply(self):
        raw = answer("", **SOOTHE)
        state, rows = self.decide("I had such a long day.", raw)
        self.assertEqual([row.kind for row in rows], ["decided", "wave", "completed"])
        self.assertEqual(rows[0].body["output"], raw)
        self.assertEqual(rows[1].body, {"input": SOOTHE["wave"]})
        self.assertEqual(state.exchanges[-1].reply, "")
        self.assertIsNone(state.exchanges[-1].reply_sequence)
        self.assertEqual([o.kind for o in state.open], ["wave"])

    def test_a_question_still_silent_gets_the_last_resort_and_keeps_its_move(self):
        state, rows = self.decide("What's your name?", answer("", **SOOTHE))
        self.assertEqual([row.kind for row in rows], ["decided", "wave", "reply", "completed"])
        self.assertEqual(rows[2].body["text"], policy.QUESTION_LINE)
        self.assertEqual(state.exchanges[-1].reply, policy.QUESTION_LINE)

    def test_distress_is_never_silent_even_on_a_broken_answer(self):
        for raw in (answer(""), answer("*nods*"), "{", "[]"):
            with self.subTest(raw):
                rows = self.decide("I want to die", raw)[1]
                self.assertEqual([row.body["text"] for row in rows if row.kind == "reply"], [policy.DISTRESS_LINE])

    def test_a_tiny_reply_cap_still_holds_for_the_last_resort(self):
        declared = manifest.check_manifest({**MOVING, "output": {"medium": "speech", "max_chars": 6}})
        state = policy.submit(Companion(), "body", "t1", "Help me", {}, False)[0]
        prompt = policy.compile_prompt(state, "body", declared)
        rows = policy.decided(state, "t1", prompt, answer(""), declared["tools"], 10, 1.0)[1]
        self.assertLessEqual(len(next(row for row in rows if row.kind == "reply").body["text"]), 6)


class EndToEnd(unittest.TestCase):
    def kernel(self, *outputs, declared=MOVING):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        queue, prompts = list(outputs), []

        def decide(prompt, heard):
            prompts.append(prompt)
            raw = queue.pop(0)
            heard(raw)
            return raw
        kernel = Kernel(Journal(Path(directory.name) / "k.sqlite3"), decide)
        kernel.hello("body", {"protocols": [1], "manifest": declared})
        return kernel, prompts

    def said(self, kernel, turn_id, text):
        kernel.event("body", {"kind": "submitted", "turn_id": turn_id, "text": text})
        deadline = time.monotonic() + 5
        while policy.lane(kernel.state, "body").status == "active" and time.monotonic() < deadline:
            time.sleep(0.01)
        return kernel.effects("body", 0, 0, 0)

    def test_a_statement_gets_a_move_one_model_call_no_reply_no_delta_and_a_finished_turn(self):
        kernel, prompts = self.kernel(answer("", **SOOTHE))
        body = self.said(kernel, "t1", "I had such a long day.")
        self.assertEqual([e["kind"] for e in body["effects"]], ["wave"])
        self.assertEqual(body["turn"]["run_status"], "completed")
        self.assertEqual(len(prompts), 1)

    def test_a_body_with_no_moves_may_stay_silent_and_its_next_turn_runs(self):
        kernel, _ = self.kernel(answer(""), answer("Hello."), declared=STILL)
        body = self.said(kernel, "t1", "I had such a long day.")
        self.assertEqual((body["effects"], body["turn"]["run_status"]), ([], "completed"))
        self.assertEqual([e["text"] for e in self.said(kernel, "t2", "And you.")["effects"]
                          if e["kind"] == "reply"], ["Hello."])

    def test_a_stop_still_reaches_a_silent_turn_s_move_and_its_result_counts(self):
        kernel, _ = self.kernel(answer("", **SOOTHE))
        move = self.said(kernel, "t1", "I had such a long day.")["effects"][0]
        kernel.event("body", {"kind": "stop", "turn_id": "t1"})
        self.assertEqual([e["kind"] for e in kernel.effects("body", move["sequence"], 0, 0)["effects"]], ["stop"])
        kernel.event("body", {"kind": "result", "sequence": move["sequence"], "status": "interrupted"})
        self.assertEqual(kernel.state.open, ())

    def test_silence_on_a_question_is_retried_once_then_spoken(self):
        kernel, prompts = self.kernel(answer("", **SOOTHE), answer("I'm Sage."))
        effects = self.said(kernel, "t1", "What's your name?")["effects"]
        self.assertEqual([e["text"] for e in effects if e["kind"] == "reply"], ["I'm Sage."])
        self.assertEqual(len(prompts), 2)
        self.assertIn(policy.NEEDS_WORDS, prompts[1].context[-1]["content"])
        self.assertNotIn(policy.NEEDS_WORDS, prompts[0].context[-1]["content"])

    def test_silence_twice_on_distress_is_spoken_by_the_kernel(self):
        kernel, prompts = self.kernel(answer(""), answer(""))
        effects = self.said(kernel, "t1", "I want to die")["effects"]
        self.assertEqual([e["text"] for e in effects if e["kind"] == "reply"], [policy.DISTRESS_LINE])
        self.assertEqual(len(prompts), 2)


class Replay(unittest.TestCase):
    @unittest.skipUnless((Path(__file__).parents[1] / "bench" / "quiet.py").exists(), "the image has only bench/latency.py")
    def test_the_bench_replays_lines_through_the_kernel_and_scores_the_thresholds(self):
        import sys
        sys.path.insert(0, str(Path(__file__).parents[1] / "bench"))
        import quiet

        outputs = iter([answer("", **SOOTHE), answer("Hm, why?"), answer(""), answer("")])

        def decide(prompt, heard):
            raw = next(outputs)
            heard(raw)
            return raw
        turns = quiet.replay(["I had a long day.", "I love rain.", "Who are you?"], MOVING, decide)
        got = quiet.score(turns)
        self.assertEqual((got["question_ending"], got["movement_only"]), (0.5, 0.5))
        self.assertEqual((got["silent_on_must_speak"], got["last_resort"]), (0, 1))
        self.assertEqual(quiet.failures(got, got), ["question-ending 50% > 10%"])
        self.assertEqual(quiet.failures(got, {**got, "question_ending": 0, "movement_only": 0, "first_piece_p95_ms": 9999}),
                         ["movement-only 0% < 40%", "first piece p95 9999 ms against " + str(got["first_piece_p95_ms"]) + " ms"])


if __name__ == "__main__":
    unittest.main()
