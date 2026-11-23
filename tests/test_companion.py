"""Sage as a companion, once a line is for it: the character a speaking body gets, an unclear or unknown thing
handled once and then left, what its context keeps, and an exact repeat that costs no second model call.
Every line here is invented."""

import json
import unittest

from test_kernel import answers, rejected, settle, started

from sage import manifest, policy
from sage.adapters.model import Answer
from sage.policy import Companion

FEEL = {"name": "emote", "description": "Show a feeling.", "max_s": 3,
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "enum": ["attentive", "happy", "curious"]}},
                        "required": ["name"]}}
BODY = {"senses": ["speech"], "tools": [FEEL]}
DECLARED = manifest.check_manifest(BODY)
HEARD = {"medium": "speech"}
UNSURE = {"medium": "speech", "confidence": 0.4}


def hear(kernel, turn_id, text, input=HEARD):
    """The body hears a line; what its turn delivered once it is over."""
    kernel.event("reachy", {"kind": "submitted", "turn_id": turn_id, "text": text, "input": input})
    settle(kernel)
    return [e for e in kernel.effects("reachy", 0, 0, 0)["effects"] if e["turn_id"] == turn_id]


def talk(*turns, device="body"):
    """The state after `turns`, each (line, raw answer or reply[, input]), and the prompt each one was asked with."""
    state, prompts = Companion(), []
    for n, (line, raw, *input) in enumerate(turns):
        state = policy.submit(state, device, f"t{n}", line, {}, False, input[0] if input else HEARD)[0]
        prompts.append(policy.compile_prompt(state, device, DECLARED))
        raw = raw if raw.startswith("{") else json.dumps({"reply": raw})
        state = policy.decided(state, f"t{n}", prompts[-1], raw, DECLARED["tools"], 10 + 10 * n, 1.0)[0]
    return state, prompts


def asked(state, line, input=HEARD, device="body"):
    """The prompt for `line`, heard next."""
    state = policy.submit(state, device, "next", line, {}, False, input)[0]
    return state, policy.compile_prompt(state, device, DECLARED)


def said(state):
    return [exchange.reply for exchange in state.exchanges]


LISTEN = {"name": "listen", "description": "Listen to what is playing.", "max_s": 20,
          "inputSchema": {"type": "object", "properties": {}}}
HEARS = manifest.check_manifest({**BODY, "tools": [FEEL, LISTEN]})


def wordless(raw, line="Listen to this.", state=None):
    """The state after `line` is answered with `raw`, and its rows as the effects a body would report on."""
    state = policy.submit(state or Companion(), "body", "t0", line, {}, False, HEARD)[0]
    state, rows = policy.decided(state, "t0", policy.compile_prompt(state, "body", HEARS), raw, HEARS["tools"], 10, 1.0)
    return state, [{"sequence": row.sequence, "kind": row.kind, "target": row.target, "turn_id": row.turn_id}
                   for row in policy.numbered(rows, 10)]


class Character(unittest.TestCase):
    def test_a_speaking_body_with_tools_is_a_companion_not_mostly_a_move(self):
        system = policy.compile_prompt(asked(Companion(), "Hi.")[0], "body", DECLARED).system
        self.assertTrue(system.startswith(policy.PERSONA))
        self.assertIn("companion", policy.PERSONA)
        self.assertNotIn("mostly answer with a move", system)
        self.assertNotIn('"reply": ""', system)  # no example teaches silence

    def test_a_body_that_declares_moves_as_before_is_a_companion_too(self):
        old = manifest.check_manifest({**BODY, "moves": {"comfort": {"emote": {"name": "happy"}}}})
        state = asked(Companion(), "Hi.")[0]
        self.assertEqual(policy.compile_prompt(state, "body", old).system, policy.compile_prompt(state, "body", DECLARED).system)

    def test_an_example_shows_only_a_feeling_this_body_has(self):
        examples = policy.compile_prompt(asked(Companion(), "Hi.")[0], "body", DECLARED).system.split("\n\n")[1]
        self.assertIn('"emote": {"name": "happy"}', examples)
        self.assertNotIn("calming", examples)  # the character's example names a feeling this body did not list
        still = manifest.check_manifest({"senses": ["speech"], "tools": []})
        self.assertNotIn("emote", policy.compile_prompt(asked(Companion(), "Hi.")[0], "body", still).system.split("\n\n")[1])

    def test_a_text_body_is_never_told_to_be_spoken(self):
        typed = manifest.check_manifest({**BODY, "output": {"medium": "text"}})
        state = policy.submit(Companion(), "body", "t", "Hi.", {}, False, {"medium": "text"})[0]
        self.assertNotIn("spoken", policy.compile_prompt(state, "body", typed).context[-1]["content"])


class Unknown(unittest.TestCase):
    ADMITTED = ("What's the tallest tree?", "I don't know the tallest tree. Do you like trees?")

    def test_the_last_resort_is_not_the_old_line_and_counts_as_not_knowing(self):
        self.assertNotEqual(policy.QUESTION_LINE, "Hmm, I don't know.")
        for line in (policy.QUESTION_LINE, policy.WATCHING_LINE, "I don't know.", "I'm not sure.", "No idea!",
                     "I really don't know that one.", "I’m not sure about that."):
            with self.subTest(line):
                self.assertTrue(policy.ADMITS.search(policy._plain(line)))
        for line in ("Canberra.", "I know that one!", "You don't know me yet.", policy.DISTRESS_LINE):
            with self.subTest(line):
                self.assertFalse(policy.ADMITS.search(policy._plain(line)))

    def test_after_admitting_once_it_is_told_not_to_admit_again(self):
        state, prompts = talk(self.ADMITTED)
        self.assertNotIn(policy.REPAIRS["unknown_again"], prompts[0].context[-1]["content"])
        newest = asked(state, "And the deepest lake?")[1].context[-1]["content"]
        self.assertIn(policy.REPAIRS["unknown_again"], newest)

    def test_a_second_admission_in_a_row_is_never_spoken_and_what_else_it_said_is(self):
        state, _ = talk(self.ADMITTED, ("And the deepest lake?", "I don't know that either. Which lake did you swim in?"))
        self.assertEqual(said(state)[-1], "Which lake did you swim in?")

    def test_a_question_answered_only_by_admitting_again_gets_a_line_that_moves_on(self):
        state, _ = talk(self.ADMITTED, ("And the deepest lake?", "I'm not sure."))
        self.assertEqual(said(state), [self.ADMITTED[1], policy.MOVE_ON_LINE])
        self.assertFalse(policy.ADMITS.search(policy._plain(policy.MOVE_ON_LINE)))
        state, _ = talk(self.ADMITTED, ("I like that lake.", "I don't know."))  # no question: nothing needs saying
        self.assertEqual(said(state), [self.ADMITTED[1]])

    def test_no_last_resort_is_spoken_twice_running_and_none_is_shown_to_the_model_as_its_own_words(self):
        state, prompts = talk(("What's the tallest tree?", ""), ("And the deepest lake?", ""), ("And the widest river?", ""))
        self.assertEqual(said(state), [policy.QUESTION_LINE, policy.MOVE_ON_LINE, policy.QUESTION_LINE])
        self.assertEqual([m["role"] for m in prompts[-1].context], ["user"])

    def test_once_it_has_said_something_else_it_may_admit_again(self):
        state, _ = talk(self.ADMITTED, ("I like oaks.", "Oaks are lovely."), ("How old is that oak?", "I don't know how old."))
        self.assertEqual(said(state)[-1], "I don't know how old.")

    def test_distress_still_gets_its_line_after_an_admission(self):
        state, _ = talk(self.ADMITTED, ("Help me, I fell.", ""))
        self.assertEqual(said(state)[-1], policy.DISTRESS_LINE)

    def test_a_garbled_question_never_gets_the_last_resort(self):
        self.assertEqual(policy.needs_words("Can you the fridge?", UNSURE), "")
        self.assertEqual(policy.needs_words("Can you the", HEARD), "")
        self.assertEqual(policy.needs_words("Can you open the fridge?", HEARD), policy.QUESTION_LINE)
        state, _ = talk(("Can you the fridge?", "", UNSURE))
        self.assertEqual(said(state), [])

    def test_the_kernel_asks_the_model_once_more_before_it_goes_silent_on_a_question(self):
        decide, prompts = answers("I don't know the tallest tree.", "I'm not sure.", "Redwoods grow tall, that much I know.")
        kernel = started(self, decide, BODY)
        hear(kernel, "u1", "What's the tallest tree?")
        self.assertEqual([e["text"] for e in hear(kernel, "u2", "And the deepest lake?") if e["kind"] == "reply"],
                         ["Redwoods grow tall, that much I know."])
        self.assertEqual(len(prompts), 3)

    def test_a_second_admission_never_streams(self):
        decide, _ = answers("I don't know the tallest tree.", ['{"reply": "I do', "n't know ", "that. ", "Tell me ", 'about it."}'])
        kernel = started(self, decide, BODY)
        hear(kernel, "u1", "What's the tallest tree?")
        self.assertEqual([e["text"] for e in hear(kernel, "u2", "And the deepest lake?") if e["kind"] == "reply"],
                         ["Tell me about it."])


    def test_a_sentence_that_admits_and_then_answers_is_spoken(self):
        """Review finding 2: the pattern anywhere in a sentence dropped the whole sentence, answer and all."""
        for reply in ("I don't know much about cars, but the Corva is solid.", "I'm not sure, maybe the one in Siberia.",
                      "It is the one in Siberia, but I'm not sure how deep."):
            with self.subTest(reply):
                state, _ = talk(self.ADMITTED, ("And the deepest lake?", reply))
                self.assertEqual(said(state)[-1], reply)
        for reply in ("Sorry, I don't know.", "Hmm, I'm not sure, no idea."):  # nothing but the admission: dropped
            with self.subTest(reply):
                state, _ = talk(self.ADMITTED, ("And the deepest lake?", reply))
                self.assertEqual(said(state)[-1], policy.MOVE_ON_LINE)

    def test_a_sentence_that_admits_and_then_answers_streams(self):
        reply = "I don't know much about cars, but the Corva is solid."
        decide, _ = answers("I don't know the tallest tree.", ['{"reply": "I do', "n't know much about cars, ",
                                                              'but the Corva is solid."}'])
        kernel = started(self, decide, BODY)
        hear(kernel, "u1", "What's the tallest tree?")
        self.assertEqual([e["text"] for e in hear(kernel, "u2", "Is the Corva any good?") if e["kind"] == "reply"], [reply])


class Unclear(unittest.TestCase):
    def test_only_what_was_heard_decides_unclear_never_a_guess_at_who_it_was_for(self):
        for line in ("Nice weather.", "Only the kettle.", "Short one.", "Age."):
            with self.subTest(line):
                self.assertFalse(policy.misheard(line, HEARD))
        for line, input in (("Can you the", HEARD), ("Open the fridge.", UNSURE), ("la la la la and we sang.", HEARD)):
            with self.subTest(line):
                self.assertTrue(policy.misheard(line, input))
        self.assertFalse(policy.misheard(policy.PROACTIVE["nudge"]))

    def test_the_newest_line_alone_is_marked_unclear(self):
        state, prompts = talk(("Can you the fridge", "Something about the fridge. What about it?"))
        self.assertIn('heard (unclear): "Can you the fridge"', prompts[0].context[-1]["content"])
        history = asked(state, "Open the fridge.")[1].context
        self.assertTrue(history[0]["content"].startswith("Can you the fridge"))
        self.assertNotIn("unclear", " ".join(m["content"] for m in history))

    def test_a_second_unclear_line_in_a_row_is_not_asked_about_again(self):
        state, prompts = talk(("Can you the fridge", "Something about the fridge. What about it?"))
        self.assertNotIn(policy.REPAIRS["unclear_again"], prompts[0].context[-1]["content"])
        state, prompt = asked(state, "The the door and")
        self.assertIn(policy.REPAIRS["unclear_again"], prompt.context[-1]["content"])
        raw = json.dumps({"reply": "Sorry, can you say that again? The fridge door, maybe."})
        state = policy.decided(state, "next", prompt, raw, DECLARED["tools"], 50, 1.0)[0]
        self.assertEqual(said(state)[-1], "The fridge door, maybe.")

    def test_a_clear_line_after_an_unclear_one_is_just_answered(self):
        state, _ = talk(("Can you the fridge", "Something about the fridge. What about it?"))
        self.assertNotIn(policy.REPAIRS["unclear_again"], asked(state, "Open the fridge.")[1].context[-1]["content"])

    def test_a_third_unclear_line_in_a_row_gets_a_move_and_the_model_is_not_asked(self):
        decide, prompts = answers("Something about the fridge. What about it?", "The fridge door, maybe.", "Yes! The door.")
        kernel = started(self, decide, BODY)
        for turn, line in (("c1", "Can you the fridge"), ("c2", "The the door and")):
            hear(kernel, turn, line)
        effects = hear(kernel, "c3", "And then the")
        self.assertEqual([(e["kind"], e.get("input")) for e in effects], [("emote", {"name": "attentive"})])
        self.assertEqual(len(prompts), 2)
        self.assertEqual([e["text"] for e in hear(kernel, "c4", "Open the fridge door.") if e["kind"] == "reply"], ["Yes! The door."])

    def test_a_sentence_that_only_has_say_in_it_is_spoken_on_a_second_unclear_line(self):
        """Review finding 2: "say that" and "say it" anywhere dropped plain sentences."""
        for reply in ("I'd say that sounds fun.", "People say it rains a lot there.",
                      "I didn't catch that, but the fridge door sounds right."):
            with self.subTest(reply):
                state, _ = talk(("Can you the fridge", "Something about the fridge. What about it?"))
                state, prompt = asked(state, "The the door and")
                state = policy.decided(state, "next", prompt, json.dumps({"reply": reply}), DECLARED["tools"], 50, 1.0)[0]
                self.assertEqual(said(state)[-1], reply)
        for reply in ("Sorry, what?", "Could you repeat that?", "I didn't catch that, say it again?"):
            with self.subTest(reply):
                self.assertEqual(policy.moved_on(reply + " The fridge door, maybe.", ("unclear_again",)),
                                 "The fridge door, maybe.")


class Asked(unittest.TestCase):
    def test_after_its_own_question_it_is_told_to_ask_none(self):
        state, prompts = talk(("I had a long day.", "Oof. Long in a good way or a bad way?"))
        self.assertNotIn(policy.REPAIRS["asked_last"], prompts[0].context[-1]["content"])
        self.assertIn(policy.REPAIRS["asked_last"], asked(state, "A bad way.")[1].context[-1]["content"])
        state, _ = talk(("I had a long day.", "Oof. That sounds tiring."))
        self.assertNotIn(policy.REPAIRS["asked_last"], asked(state, "It was.")[1].context[-1]["content"])

    def test_a_note_of_the_kernel_s_own_gets_no_repair_note(self):
        state, _ = talk(("I had a long day.", "Oof. Long in a good way or a bad way?"))
        state = policy.start_proactive(state, "body", "k1", "nudge")[0]
        self.assertEqual(policy.repairs(state, "body"), ())

    def test_a_bare_yes_when_it_had_not_asked_is_a_move_and_one_to_its_question_is_an_answer(self):
        told, _ = talk(("I had a long day.", "Oof. That sounds tiring."))
        self.assertEqual(json.loads(policy.move_only(asked(told, "Yeah.")[0], "body", DECLARED["tools"])),
                         {"reply": "", "emote": {"name": "attentive"}})
        asking, _ = talk(("I had a long day.", "Oof. Long in a good way or a bad way?"))
        self.assertEqual(policy.move_only(asked(asking, "Yeah.")[0], "body", DECLARED["tools"]), "")
        self.assertEqual(policy.move_only(asked(told, "Nice weather.")[0], "body", DECLARED["tools"]), "")
        self.assertEqual(policy.move_only(asked(told, "Help.")[0], "body", DECLARED["tools"]), "")

class Context(unittest.TestCase):
    def test_a_turn_it_answered_with_no_words_is_not_in_its_context(self):
        state, _ = talk(("I had a long day.", json.dumps({"reply": "", "emote": {"name": "happy"}})),
                        ("My cat is asleep.", "Cats know how to rest."))
        self.assertEqual([x.user for x in state.exchanges if policy.told(x)], ["My cat is asleep."])
        context = asked(state, "She is.")[1].context
        self.assertEqual([m["role"] for m in context], ["user", "assistant", "user"])

    def test_the_last_resort_is_never_shown_to_the_model_as_its_own_words(self):
        state, _ = talk(("What's the tallest tree?", ""), ("I like oaks.", "Oaks are lovely."))
        shown = " ".join(m["content"] for m in asked(state, "They are.")[1].context)
        self.assertNotIn(policy.QUESTION_LINE, shown)
        self.assertNotIn("tallest tree", shown)
        self.assertIn("Oaks are lovely.", shown)

    def test_a_reply_interrupted_before_any_word_is_not_kept_and_one_cut_short_is(self):
        state = policy.submit(Companion(), "body", "t1", "Tell me a story.", {}, False, HEARD)[0]
        silent = policy.report_turn(state, "body", "t1", None, "interrupted", "", None, {})[0]
        self.assertEqual(silent.exchanges, ())
        cut = policy.report_turn(state, "body", "t1", None, "interrupted", "Once there", None, {})[0]
        self.assertEqual(said(cut), ["Once there"])

    def test_what_the_body_did_on_its_own_waits_for_the_next_turn_it_speaks_in(self):
        state = policy.act(Companion(), "body", {"act_id": "a1", "origin": "grammar", "what": "aside", "status": "done",
                                                 "said": "Looking left."})[0]
        state = policy.submit(state, "body", "t1", "Hm.", {}, False, HEARD)[0]
        prompt = policy.compile_prompt(state, "body", DECLARED)
        state = policy.decided(state, "t1", prompt, json.dumps({"reply": ""}), DECLARED["tools"], 10, 1.0)[0]
        self.assertEqual(len(state.acts), 1)

    def test_an_old_checkpoint_s_silent_turns_are_dropped_when_it_is_read(self):
        state, _ = talk(("I like oaks.", "Oaks are lovely."))
        data = json.loads(json.dumps(policy.encode(state)))
        data["exchanges"].insert(0, {**data["exchanges"][0], "reply": "", "user": "Hm."})
        self.assertEqual(said(policy.decode(data)), ["Oaks are lovely."])


    def test_a_turn_with_no_words_whose_tool_reported_is_told_with_what_the_tool_did(self):
        """Review finding 3: the exchange was dropped, so the tool's result settled onto nothing."""
        state, effects = wordless(json.dumps({"reply": "", "listen": {}}))
        self.assertEqual([m["role"] for m in asked(state, "Hm.", device="body")[1].context], ["user"])  # still listening
        self.assertEqual(policy.decode(json.loads(json.dumps(policy.encode(state)))), state)
        state = policy.report(state, "body", effects[1], "done", "A slow song about rain.", None, {})[0]
        self.assertEqual(policy.decode(json.loads(json.dumps(policy.encode(state)))), state)
        context = asked(state, "What was it?")[1].context
        self.assertEqual([m["role"] for m in context], ["user", "assistant", "tool", "user"])
        self.assertTrue(context[0]["content"].startswith("Listen to this."))
        self.assertEqual(json.loads(context[1]["content"]), {"reply": "", "listen": {}})
        self.assertEqual(context[2]["content"], "(your body body: listen done: A slow song about rain.)")
        self.assertEqual(policy.last_replies(state, "body"), ())  # no words: nothing to repeat or repair

    def test_a_turn_with_no_words_whose_tool_failed_or_was_refused_is_told(self):
        state, effects = wordless(json.dumps({"reply": "", "listen": {}}))
        state = policy.report(state, "body", effects[1], "failed", None, "no microphone", {})[0]
        self.assertEqual(asked(state, "What was it?")[1].context[2]["content"],
                         "(your body body could not do listen: no microphone)")
        state, _ = wordless(json.dumps({"reply": "", "emote": {"name": "furious"}}))
        self.assertEqual([m["role"] for m in asked(state, "Well?")[1].context], ["user", "assistant", "tool", "user"])

    def test_a_turn_with_no_words_whose_move_just_happened_is_let_go(self):
        """A feeling shown and nothing said is still not conversation: its empty reply would be copied."""
        state, effects = wordless(json.dumps({"reply": "", "emote": {"name": "happy"}}), "I had a long day.")
        state = policy.report(state, "body", effects[1], "done", None, None, {})[0]
        self.assertEqual((state.exchanges, state.open), ((), ()))

    def test_what_the_body_did_on_its_own_is_told_once_when_a_wordless_turn_is_told_later(self):
        state = policy.act(Companion(), "body", {"act_id": "a1", "origin": "grammar", "what": "aside", "status": "done",
                                                 "said": "Looking left."})[0]
        state, effects = wordless(json.dumps({"reply": "", "listen": {}}), state=state)
        state = policy.report(state, "body", effects[1], "done", "A slow song about rain.", None, {})[0]
        shown = json.dumps(asked(state, "What was it?")[1].context)
        self.assertEqual(shown.count("Looking left."), 1)


class Repeats(unittest.TestCase):
    def test_only_the_same_words_are_a_repeat(self):
        recent = ("it is good to hear you",)
        self.assertTrue(policy.repeats(recent, "It is good to hear you!"))
        self.assertFalse(policy.repeats(recent, "It is good to hear from you again."))  # the same opening: new words
        self.assertTrue(policy.may_repeat(recent, "It is good"))
        self.assertFalse(policy.may_repeat(recent, "It is good to hear from"))

    def test_an_exact_repeat_on_a_statement_is_its_move_alone_with_no_second_call(self):
        again = json.dumps({"reply": "Oaks are lovely.", "emote": {"name": "happy"}})
        decide, prompts = answers("Oaks are lovely.", [again])
        kernel = started(self, decide, BODY)
        hear(kernel, "r1", "I like oaks.")
        effects = hear(kernel, "r2", "I really like oaks.")
        self.assertEqual([(e["kind"], e.get("input")) for e in effects], [("emote", {"name": "happy"})])
        self.assertEqual((len(prompts), rejected(kernel)), (2, ["repeat of a recent reply"]))

    def test_the_same_opening_with_new_words_is_said_and_costs_one_call(self):
        decide, prompts = answers("It is good to hear you.", ['{"reply": "It ', "is ", "good ", "to ", "hear ", "from ", 'you again."}'])
        kernel = started(self, decide, BODY)
        hear(kernel, "r1", "Hello again.")
        self.assertEqual([e["text"] for e in hear(kernel, "r2", "Hello once more.")], ["It is good to hear from you again."])
        self.assertEqual((len(prompts), rejected(kernel)), (2, []))

    def test_the_same_question_again_gets_the_same_answer_again(self):
        decide, prompts = answers("I'm Sage!", "I'm Sage!")
        kernel = started(self, decide, BODY)
        hear(kernel, "r1", "What's your name?")
        self.assertEqual([e["text"] for e in hear(kernel, "r2", "What is your name?")], ["I'm Sage!"])
        self.assertEqual((len(prompts), rejected(kernel)), (2, []))


class Score(unittest.TestCase):
    """evals/companion.py: the design's measures on a journal's lines, with no model."""

    def test_a_journal_s_answers_are_scored_as_recorded_and_again_through_this_kernel(self):
        from evals import companion
        decide, _ = answers("I don't know the tallest tree.", "I don't know that either. Which lake did you swim in?",
                            "Oaks are lovely.", "Oaks are lovely.")

        def timed(prompt, heard):  # as the Ollama provider answers: the journal knows a model's answer by its timing
            answer = Answer(decide(prompt, heard))
            answer.timing = {"prompt_tokens": 900}
            return answer
        kernel = started(self, timed, BODY)
        for n, line in enumerate(("What's the tallest tree?", "And the deepest lake?", "I like oaks.", "I really like oaks.")):
            hear(kernel, f"s{n}", line)
        turns = companion.recorded(kernel.journal.path)
        self.assertEqual([t["line"] for t in turns][:2], ["What's the tallest tree?", "And the deepest lake?"])
        self.assertEqual([t["calls"] for t in turns], [1, 1, 1, 1])
        for t in turns[1:]:  # as the kernel that wrote the journal said it: the old one, which said it all
            t["reply"] = json.loads(t["output"])["reply"]
        before, after = companion.score(turns), companion.score(companion.replay(turns, BODY))
        self.assertEqual((before["spoken"], before["dont_know"], before["dont_know_twice_running"]), (4, 0.5, 1))
        self.assertEqual((after["spoken"], after["dont_know"], after["dont_know_twice_running"]), (3, 0.333, 0))
        self.assertEqual((after["questions"], after["questions_dropped"], after["second_calls"]), (2, 0, 0.0))
        self.assertGreater(after["prompt_chars_median"], len(policy.PERSONA))

    def test_the_bars_are_the_design_s_and_a_miss_is_named(self):
        from evals import companion
        got = {name: bar for name, (_, bar) in companion.BARS.items()}
        self.assertEqual(companion.misses(got), [])
        self.assertEqual(companion.misses({**got, "dont_know": 0.54}), ["dont_know 0.54 is not at most 0.15"])
        self.assertEqual(companion.misses({**got, "statements_with_words": 0.4}),
                         ["statements_with_words 0.4 is not at least 0.9"])


if __name__ == "__main__":
    unittest.main()
