"""The kernel's pure policy: turns, memory, authorization, results, limits. No clock, file or network."""

import json
import unittest
from dataclasses import replace

from sage import manifest, policy
from sage.policy import Companion, Refused

EMOTE = {"name": "emote", "description": "Show a feeling.", "max_s": 3, "interruptible": True,
         "inputSchema": {"type": "object", "properties": {"name": {"enum": ["cheerful1", "sad1"]}},
                         "required": ["name"]}}
TOOLS = (EMOTE,)
MANIFEST = {"senses": ("speech",), "tools": TOOLS}


def kinds(rows):
    return [(row.kind, row.target) for row in rows]


def asked(text="Hi", source="reachy", turn_id="r1", state=None):
    return policy.submit(state or Companion(), source, turn_id, text, {}, seen=False)[0]


def output(reply, **actions):
    return json.dumps({"reply": reply, **actions})


HELLO = output("Hello.")


def answered(state, raw=HELLO, tools=TOOLS, sequence=10, now=100.0):
    prompt = policy.compile_prompt(state, "reachy", {"senses": ("speech",), "tools": tools})
    return policy.decided(state, state.turn.turn_id, prompt, raw, tools, sequence, now)


def newest(state, text="Again?", turn_id="r9"):  # the newest message
    return policy.compile_prompt(asked(text, turn_id=turn_id, state=state), "reachy", MANIFEST).context[-1]["content"]


def body_notes(state):
    """What the body reported about its own actions, as the model sees it on the next turn."""
    context = policy.compile_prompt(asked("Again?", turn_id="r9", state=state), "reachy", MANIFEST).context
    return [m["content"] for m in context if m["role"] == "tool"]


class Turns(unittest.TestCase):
    def test_a_submission_starts_a_turn_and_a_retry_admits_nothing(self):
        state, rows = policy.submit(Companion(), "reachy", "r1", "Hi", {"stt_ms": 5}, seen=False)
        self.assertEqual((state.turn.turn_id, state.turn.source, state.turn.status), ("r1", "reachy", "active"))
        self.assertEqual(kinds(rows), [("submitted", None)])
        self.assertEqual(policy.submit(state, "reachy", "r1", "Hi", {}, seen=True), (state, ()))

    def test_one_turn_at_a_time_and_an_interrupted_one_blocks_until_acknowledged(self):
        running = asked()
        with self.assertRaises(Refused) as refused:
            policy.submit(running, "mock", "m1", "Hi", {}, seen=False)
        self.assertEqual(refused.exception.code, "busy")
        cut, rows = policy.restarted(running)
        self.assertEqual((cut.turn.status, kinds(rows)), ("unknown", [("restarted", None)]))
        with self.assertRaises(Refused) as refused:
            policy.submit(cut, "reachy", "r2", "Hi", {}, seen=False)
        self.assertEqual(refused.exception.code, "unknown_pending")
        with self.assertRaises(Refused) as refused:
            policy.acknowledge(cut, "mock", "r1")
        self.assertEqual(refused.exception.code, "forbidden")
        clear, rows = policy.acknowledge(cut, "reachy", "r1")
        self.assertEqual((clear.turn.status, kinds(rows)), ("none", [("acknowledged", None)]))
        self.assertEqual(policy.acknowledge(clear, "reachy", "r1"), (clear, ()))  # a retry is safe
        self.assertEqual(policy.submit(clear, "reachy", "r2", "Hi", {}, seen=False)[0].turn.status, "active")

    def test_a_completed_turn_delivers_its_tools_and_its_reply_each_with_its_own_sequence(self):
        state, rows = answered(asked("Feel happy"), output("Yay cheerful1!", emote={"name": "cheerful1"}))
        self.assertEqual(kinds(rows), [("decided", None), ("emote", "reachy"), ("reply", "reachy"),
                                       ("completed", None)])
        self.assertEqual((rows[1].body, rows[2].body), ({"input": {"name": "cheerful1"}}, {"text": "Yay !"}))
        self.assertEqual([(o.sequence, o.kind, o.deadline) for o in state.open],
                         [(11, "emote", 100.0 + 3 + 2), (12, "reply", 100.0 + 30)])
        self.assertEqual(state.turn.status, "completed")
        self.assertEqual(state.exchanges[-1].reply_sequence, 12)

    def test_a_reply_never_speaks_a_tool_s_syntax(self):
        _, rows = answered(asked(), output("Sure (emote: ) thing."))
        self.assertEqual(rows[1].body, {"text": "Sure thing."})

    def test_silence_is_a_decision(self):
        state, rows = answered(asked(), output(""))
        self.assertEqual(kinds(rows), [("decided", None), ("completed", None)])
        self.assertEqual(state.open, ())

    def test_a_stopped_turn_records_its_decision_but_delivers_nothing(self):
        state = asked()
        prompt = policy.compile_prompt(state, "reachy", MANIFEST)
        stopped, rows = policy.stop(state, "mock", None)
        self.assertEqual((stopped.turn.status, kinds(rows)), ("cancelled", [("stop", None)]))
        after, rows = policy.decided(stopped, "r1", prompt, output("Hello."), TOOLS, 10, 100.0)
        self.assertEqual(kinds(rows), [("decided", None)])
        self.assertEqual(rows[0].body["output"], output("Hello."))
        self.assertEqual(after.exchanges, ())

    def test_a_model_failure_is_recorded_with_what_it_was_given_and_frees_the_turn(self):
        state = asked()
        prompt = policy.compile_prompt(state, "reachy", MANIFEST)
        failed, rows = policy.failed(state, "r1", prompt, "ConnectionRefusedError")
        self.assertEqual((failed.turn.status, kinds(rows)), ("failed", [("failed", None)]))
        self.assertIn("Hi", rows[0].body["prompt"]["context"][-1]["content"])
        self.assertEqual(policy.submit(failed, "reachy", "r2", "Hi", {}, seen=False)[0].turn.status, "active")


class Authorization(unittest.TestCase):
    def test_only_declared_tools_with_valid_input_are_delivered_and_the_rest_reach_the_model_as_errors(self):
        state, rows = answered(asked("Wave"), output("Hi!", wave={}, emote={"name": "angry"}))
        self.assertEqual(kinds(rows), [("decided", None), ("reply", "reachy"), ("completed", None)])
        notes = body_notes(state)
        self.assertEqual(len(notes), 1)
        self.assertIn("could not do wave: unavailable", notes[0])
        self.assertIn("could not do emote: invalid input", notes[0])
        self.assertEqual(newest(state), "Again?" + policy.reminder(MANIFEST))  # never inside the person's words

    def test_input_schema_subset(self):
        schema = {"type": "object", "required": ["n"], "additionalProperties": False,
                  "properties": {"n": {"type": "integer"}, "tags": {"type": "array", "items": {"type": "string"}}}}
        self.assertEqual(manifest.check(schema, {"n": 2, "tags": ["a"]}), "")
        for bad in ({}, {"n": "2"}, {"n": True}, {"n": 1.5}, {"n": 1, "x": 0}, {"n": 1, "tags": [3]}, []):
            with self.subTest(bad=bad):
                self.assertTrue(manifest.check(schema, bad))

    def test_a_malformed_manifest_is_refused(self):
        self.assertEqual(manifest.check_manifest({"senses": ["speech"], "tools": [EMOTE]}), MANIFEST)
        self.assertEqual(manifest.check_manifest({})["tools"], ())
        for bad in ([], {"tools": EMOTE}, {"tools": [{**EMOTE, "name": "reply"}]},
                    {"tools": [{**EMOTE, "name": "Wave!"}]}, {"tools": [{**EMOTE, "max_s": "soon"}]},
                    {"tools": [{k: v for k, v in EMOTE.items() if k != "inputSchema"}]},
                    {"tools": [EMOTE, EMOTE]}, {"tools": [{**EMOTE, "name": f"t{i}"} for i in range(65)]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                manifest.check_manifest(bad)

    def test_the_model_answers_in_a_reply_first_format_of_its_device_tools(self):
        schema = manifest.format_schema(TOOLS)
        self.assertEqual(list(schema["properties"]), ["reply", "emote", "remember", "forget"])
        # Brevity is decoded, not asked for; no pattern, so digits stay speakable.
        self.assertEqual(schema["properties"]["reply"], {"type": "string", "maxLength": 160})
        self.assertEqual(schema["properties"]["emote"], EMOTE["inputSchema"])
        self.assertEqual((schema["required"], schema["additionalProperties"]), (["reply"], False))
        self.assertEqual(list(manifest.format_schema(())["properties"]), ["reply", "remember", "forget"])

    def test_labels_are_the_declared_names_that_are_not_words(self):
        tools = (EMOTE, {"name": "look", "inputSchema": {"type": "object", "properties": {
            "side": {"enum": ["left", "right"]}, "mood": {"items": {"enum": ["happy_2"]}}}}})
        self.assertEqual(manifest.labels(tools), ("cheerful1", "sad1", "happy_2"))


class Results(unittest.TestCase):
    def setUp(self):
        self.state, rows = answered(asked("Count"), output("One two three four five six.", emote={"name": "sad1"}))
        self.emote, self.reply = ({"sequence": row.sequence, "kind": row.kind, "target": row.target,
                                   "turn_id": row.turn_id} for row in policy.numbered(rows[1:3], 11))

    def report(self, effect, status, text=None, reason=None, timing=None, source="reachy", state=None):
        return policy.report(state or self.state, source, effect, status, text, reason, timing or {})

    def test_an_interrupted_reply_is_remembered_as_only_what_was_heard(self):
        state, rows = self.report(self.reply, "interrupted", "One two")
        self.assertEqual(state.exchanges[-1].reply, "One two")
        context = json.dumps(policy.compile_prompt(asked("Go on", state=state), "reachy", MANIFEST).context)
        self.assertIn("One two", context)
        self.assertNotIn("five six", context)
        self.assertEqual(kinds(rows), [("result", None)])

    def test_an_interrupted_reply_is_noted_outside_the_model_s_own_words(self):
        """Live: {"reply": "I'll (interrupted)"} in history made the model say "(interrupted)" aloud."""
        state, _ = self.report(self.reply, "interrupted", "One two")
        context = policy.compile_prompt(asked("Go on", state=state), "reachy", MANIFEST).context
        self.assertEqual(json.loads(context[1]["content"])["reply"], "One two")
        self.assertEqual(context[2], {"role": "tool", "content": '(the person interrupted you after: "One two")'})
        streamed, _ = policy.report_turn(asked("Count"), "reachy", "r1", None, "interrupted", "", None, {})
        context = policy.compile_prompt(asked("Go on", turn_id="r2", state=streamed), "reachy", MANIFEST).context
        self.assertEqual(json.loads(context[1]["content"])["reply"], "")
        self.assertEqual(context[2]["content"], "(the person interrupted you before you said anything)")
        self.assertNotIn("interrupted", json.dumps([m for m in context if m["role"] == "assistant"]))
        self.assertEqual(policy.recent_replies(state, "reachy"), ("one two",))  # the mark is not speech

    def test_the_first_result_counts_and_only_from_the_device_it_was_delivered_to(self):
        with self.assertRaises(Refused) as refused:
            self.report(self.reply, "done", source="mock")
        self.assertEqual(refused.exception.code, "forbidden")
        with self.assertRaises(Refused) as refused:
            self.report(None, "done")
        self.assertEqual(refused.exception.code, "invalid")
        state, _ = self.report(self.reply, "interrupted", "One")
        self.assertEqual(self.report(self.reply, "interrupted", "One two", state=state), (state, ()))

    def test_a_tool_with_no_result_after_max_s_and_grace_fails_as_timeout_and_the_model_hears_why(self):
        self.assertEqual(policy.sweep(self.state, 100.0 + 5), (self.state, ()))
        state, rows = policy.sweep(self.state, 100.0 + 5.01)
        self.assertEqual([row.body for row in rows], [{"sequence": 11, "status": "failed", "reason": "timeout"}])
        self.assertEqual([o.kind for o in state.open], ["reply"])
        self.assertEqual(body_notes(state), ["(your reachy body could not do emote: timeout)"])
        self.assertEqual(self.report(self.emote, "done", state=state), (state, ()))  # too late

    def test_a_device_reason_reaches_the_model(self):
        state, _ = self.report(self.emote, "failed", reason="motor_hot")
        self.assertEqual(body_notes(state), ["(your reachy body could not do emote: motor_hot)"])

    def test_actions_that_went_well_are_not_reported(self):
        state, _ = self.report(self.emote, "done")
        self.assertEqual(body_notes(state), [])
        self.assertEqual(body_notes(self.state), [])  # nor those still running

    def test_a_reply_with_no_result_times_out_30_s_after_it_was_delivered(self):
        state, rows = policy.sweep(self.state, 100.0 + 30.01)
        self.assertEqual(state.open, ())
        self.assertEqual(len(rows), 2)

    def test_an_effect_s_duration_is_kept_with_the_turn_keyed_by_its_sequence(self):
        for bad in (-1, 600001, 1.5, "1830", True):
            with self.subTest(duration_ms=bad), self.assertRaises(Refused):
                policy.check_event({"kind": "result", "sequence": 12, "status": "done", "duration_ms": bad})
        self.assertEqual(policy.check_event({"kind": "result", "sequence": 12, "status": "done", "duration_ms": 0}),
                         "result")
        state, rows = policy.report(self.state, "reachy", self.emote, "done", None, None, {}, duration_ms=1830)
        state, _ = policy.report(state, "reachy", self.reply, "done", None, None, {"ttft_ms": 5}, duration_ms=600000)
        self.assertEqual(state.turn.timing, {"durations_ms": {"11": 1830, "12": 600000}, "ttft_ms": 5})
        self.assertEqual(rows[0].body["duration_ms"], 1830)

    def test_device_timing_is_merged_into_the_turn(self):
        state = replace(self.state, turn=replace(self.state.turn, timing={"stt_ms": 120}))
        state, _ = self.report(self.reply, "done", timing={"first_audio_ms": 650}, state=state)
        self.assertEqual(state.turn.timing, {"stt_ms": 120, "first_audio_ms": 650})


class Streaming(unittest.TestCase):
    def test_an_interrupted_result_naming_a_streaming_turn_ends_it_as_what_was_heard(self):
        state = asked("Count to six")
        with self.assertRaises(Refused) as refused:
            policy.report_turn(state, "mock", "r1", None, "interrupted", "One", None, {})
        self.assertEqual(refused.exception.code, "forbidden")
        ended, rows = policy.report_turn(state, "reachy", "r1", None, "interrupted", "One two", None, {})
        self.assertEqual((ended.turn.status, ended.exchanges[-1].reply, kinds(rows)),
                         ("interrupted", "One two", [("result", None)]))
        self.assertEqual(policy.report_turn(ended, "reachy", "r1", None, "interrupted", "One two", None, {}),
                         (ended, ()))  # a retry changes nothing
        prompt = policy.compile_prompt(ended, "reachy", MANIFEST)
        self.assertEqual(kinds(policy.decided(ended, "r1", prompt, output("One two three."), TOOLS, 10, 1.0)[1]),
                         [("decided", None)])  # the model finishing later delivers nothing
        with self.assertRaises(Refused) as refused:
            policy.report_turn(ended, "reachy", "r0", None, "interrupted", "", None, {})
        self.assertEqual(refused.exception.code, "invalid")

    def test_a_streaming_reply_s_duration_is_kept_under_reply(self):
        ended, _ = policy.report_turn(asked("Count"), "reachy", "r1", None, "interrupted", "One", None, {},
                                      duration_ms=900)
        self.assertEqual(ended.turn.timing, {"durations_ms": {"reply": 900}})

    def test_a_turn_id_result_for_a_delivered_reply_is_that_reply_s_result(self):
        state, _ = answered(asked())
        reply = {"sequence": 11, "kind": "reply", "target": "reachy", "turn_id": "r1"}
        settled, _ = policy.report_turn(state, "reachy", "r1", reply, "interrupted", "Hel", None, {})
        self.assertEqual((settled.open, settled.exchanges[-1].reply), ((), "Hel"))


class Stop(unittest.TestCase):
    def test_stop_reaches_each_acting_device_and_needs_no_result(self):
        state, _ = answered(asked())
        state, rows = policy.stop(state, "mock", None)
        self.assertEqual(kinds(rows), [("stop", None), ("stop", "reachy")])
        self.assertEqual(rows[0].source, "mock")  # journaled with its sender
        idle = replace(state, open=())
        self.assertEqual(kinds(policy.stop(idle, "reachy", None)[1]), [("stop", None)])

    def test_a_streaming_device_is_acting(self):
        state, rows = policy.stop(asked(), "mock", None, streaming=True)
        self.assertEqual((state.turn.status, kinds(rows)), ("cancelled", [("stop", None), ("stop", "reachy")]))

    def test_a_stop_for_a_turn_neither_running_nor_acted_on_is_stale(self):
        running = asked()
        with self.assertRaises(Refused) as refused:
            policy.stop(running, "mock", "r0")
        self.assertEqual(refused.exception.code, "stale")
        self.assertEqual(policy.stop(running, "mock", "r1")[0].turn.status, "cancelled")
        delivered, _ = answered(running)
        self.assertEqual(kinds(policy.stop(delivered, "mock", "r1")[1]), [("stop", None), ("stop", "reachy")])


class Provenance(unittest.TestCase):
    def test_the_prompt_is_one_frozen_value_with_a_stable_start(self):
        first = policy.compile_prompt(asked(), "reachy", MANIFEST)
        state, _ = answered(asked())
        second = policy.compile_prompt(asked("And now?", turn_id="r2", state=state), "reachy", MANIFEST)
        self.assertEqual((first.system, first.format), (second.system, second.format))
        self.assertEqual(first.messages()[0], {"role": "system", "content": first.system})
        self.assertEqual(second.context[1], {"role": "assistant", "content": output("Hello.")})
        with self.assertRaises(AttributeError):
            first.system = "changed"

    def test_history_is_byte_stable_so_the_model_server_reuses_its_cache(self):
        state = asked("Feel happy")
        before = policy.compile_prompt(state, "reachy", MANIFEST)
        state, _ = answered(state, output("Yay!", emote={"name": "cheerful1"}))
        after = policy.compile_prompt(asked("And now?", turn_id="r2", state=state), "reachy", MANIFEST)
        self.assertEqual(after.messages()[0], before.messages()[0])  # the cached start
        # Every message is sent again as it was, but for the newest's tool reminder.
        self.assertEqual(after.context[0]["content"] + policy.reminder(MANIFEST), before.context[0]["content"])
        state, _ = answered(asked("And now?", turn_id="r2", state=state))
        later = policy.compile_prompt(asked("Then?", turn_id="r3", state=state), "reachy", MANIFEST)
        self.assertEqual(later.context[:2], after.context[:2])

    def test_the_newest_message_reminds_the_model_of_its_tools_and_its_character(self):
        """Persona fidelity decays over a dialogue: a one-line reminder rides on the newest message only."""
        self.assertEqual(newest(Companion(), "Hi"), "Hi\n\n" + policy.STAY + "\n[JSON fields you may add: emote]")
        bare = policy.compile_prompt(asked("Hi", source="mock"), "mock", {"senses": (), "tools": ()})
        self.assertEqual(bare.context[-1]["content"], "Hi\n\n" + policy.STAY)
        state, _ = answered(asked("Hi"))
        history = policy.compile_prompt(asked("Again?", turn_id="r2", state=state), "reachy", MANIFEST).context[0]
        self.assertEqual(history["content"], "Hi")

    def test_the_fixed_prefix_holds_a_short_character_sheet_and_in_character_examples(self):
        system = policy.compile_prompt(asked("Hi"), "reachy", MANIFEST).system
        self.assertTrue(system.startswith(policy.PERSONA + "\n\n" + policy.EXAMPLES))
        examples = [json.loads(line.split(" -> ", 1)[1]) for line in policy.EXAMPLES.splitlines()[1:]]
        self.assertTrue(2 <= len(examples) <= 4)
        self.assertTrue(all(next(iter(example)) == "reply" for example in examples))  # reply first
        self.assertEqual([key for example in examples for key in example if key != "reply"], ["remember"])
        self.assertLess(len(policy.PERSONA), 700)

    def test_the_prompt_keeps_a_short_window_of_exchanges_cut_back_in_blocks(self):
        """Ollama re-reads everything after the first changed message: a window sliding by one every turn
        re-read the whole history every turn (prefill 0.4 s to 1.5 s, qwen3:4b-instruct). Cut back in blocks,
        the history's start holds for several turns."""
        state, kept, starts = Companion(), [], []
        for index in range(20):
            state, _ = answered(asked(f"line {index}", turn_id=f"r{index}", state=state), output(f"Reply {index}."))
            kept.append(len(state.exchanges))
            starts.append(state.exchanges[0].user)
        self.assertEqual((policy.WINDOW, policy.KEPT), (10, 6))
        self.assertEqual(kept, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 6, 7, 8, 9, 10, 6, 7, 8, 9, 10])
        self.assertEqual(len(set(starts)), 3)  # the history's start moved twice in 20 turns

    def test_the_body_note_says_plainly_what_the_body_cannot_sense(self):
        note = manifest.describe("reachy", MANIFEST)  # senses: speech only
        self.assertIn("This body cannot see or feel touch; if asked, say so.", note)
        self.assertNotIn("cannot hear", note)
        everything = manifest.describe("x", {"senses": ("speech", "vision", "touch"), "tools": ()})
        self.assertNotIn("cannot", everything)
        self.assertIn("cannot see, hear or feel touch", manifest.describe("x", {"senses": (), "tools": ()}))

    def test_the_body_note_follows_the_persona_once_per_device(self):
        reachy = policy.compile_prompt(asked("Hi there"), "reachy", MANIFEST)
        self.assertTrue(reachy.system.startswith(policy.PERSONA))
        for needle in ("reachy", "speech", "emote", "Show a feeling.", "cheerful1"):
            self.assertIn(needle, reachy.system)
        self.assertTrue(reachy.context[-1]["content"].startswith("Hi there"))
        mock = policy.compile_prompt(asked("Hi there", source="mock"), "mock", {"senses": (), "tools": ()})
        self.assertNotIn("emote", mock.system)

    def test_a_checkpoint_from_before_devices_and_failure_only_outcomes_reads_correctly(self):
        """Live run 2 replayed round-2 exchanges as "(your  body could not do emote: emote: done)"."""
        old = {"turn": {"turn_id": "r3", "source": "reachy", "status": "completed", "text": "", "timing": {}},
               "open": [], "exchanges": [
                   {"user": "Hi", "reply": "I'm curious! You said hi.", "reply_sequence": 4, "repeat": False, "calls": [
                       {"name": "emote", "input": {"name": "sad1"}, "sequence": 3, "outcome": "emote: done"}]},
                   {"user": "Yo", "reply": "I'm curious! You said yo.", "reply_sequence": 8, "calls": [
                       {"name": "emote", "input": {"name": "sad1"}, "sequence": 7, "outcome": "emote: interrupted"},
                       {"name": "wave", "input": {}, "sequence": None,
                        "outcome": "wave: unavailable (this body has no such tool; use only the tools it lists)"}]}]}
        state = policy.decode(old)
        self.assertEqual(body_notes(state), [("(your body could not do emote: interrupted; could not do wave: "
                                              "unavailable (this body has no such tool; use only the tools it lists))")])
        self.assertTrue(policy.repeats(policy.recent_replies(state, "reachy"), "I'm curious! You said hello."))
        self.assertEqual(state.facts, ())  # from before remember

    def test_the_checkpoint_round_trips(self):
        state, _ = answered(asked("Feel happy"), output("Yay!", emote={"name": "cheerful1"}, wave={}, remember="Likes tea."))
        self.assertEqual(state.facts, ("Likes tea.",))
        self.assertEqual(policy.decode(json.loads(json.dumps(policy.encode(state)))), state)


class Memory(unittest.TestCase):
    """Short stable facts the model keeps with remember and drops with forget, as Pollen's conversation app does."""

    def test_a_remembered_fact_is_journaled_and_stays_in_the_fixed_prefix_past_the_window(self):
        state, rows = answered(asked("My name is Juno."), output("Hi Juno!", remember="Their name is Juno."))
        self.assertIn(("remember", None), kinds(rows))  # journaled, delivered to no device
        for index in range(policy.WINDOW + 4):
            state, _ = answered(asked(f"line {index}", turn_id=f"r{index}", state=state), output(f"Reply {index}."))
        self.assertNotIn("My name is Juno.", [x.user for x in state.exchanges])
        prompt = policy.compile_prompt(asked("What's my name?", turn_id="rx", state=state), "reachy", MANIFEST)
        self.assertTrue(prompt.system.endswith("\n- Their name is Juno."), prompt.system)
        self.assertNotIn("Juno", json.dumps(prompt.context))

    def test_facts_are_capped_deduplicated_and_kept_in_the_order_they_were_learned(self):
        state = Companion()
        for index in range(policy.MAX_FACTS + 2):
            state, _ = answered(asked(turn_id=f"r{index}", state=state), output("Okay.", remember=f"Fact  {index}. "))
        state, _ = answered(asked(turn_id="again", state=state), output("Okay.", remember="fact 5."))
        self.assertEqual(state.facts, tuple(f"Fact {i}." for i in range(2, policy.MAX_FACTS + 2)))

    def test_forget_drops_every_fact_holding_its_words_and_a_blank_one_drops_nothing(self):
        state, _ = answered(asked(), output("Okay.", remember="Has a dog named Mochi."))
        state, _ = answered(asked(turn_id="r2", state=state), output("Okay.", remember="Likes tea."))
        state, rows = answered(asked(turn_id="r3", state=state), output("Done.", forget="mochi"))
        self.assertEqual((state.facts, ("forget", None) in kinds(rows)), (("Likes tea.",), True))
        state, rows = answered(asked(turn_id="r4", state=state), output("Hm.", forget=" "))
        self.assertEqual((state.facts, ("forget", None) in kinds(rows)), (("Likes tea.",), False))
        self.assertEqual(body_notes(state), [])

    def test_the_model_sees_its_own_remember_in_its_past_answers(self):
        state, _ = answered(asked("I'm Juno."), output("Hi Juno!", remember="Their name is Juno."))
        assistant = policy.compile_prompt(asked(turn_id="r2", state=state), "reachy", MANIFEST).context[1]
        self.assertEqual(json.loads(assistant["content"]), {"reply": "Hi Juno!", "remember": "Their name is Juno."})

    def test_a_device_tool_named_remember_is_the_device_s(self):
        own = {"name": "remember", "inputSchema": {"type": "object"}}
        self.assertEqual(manifest.format_schema((own,))["properties"]["remember"], own["inputSchema"])
        state, rows = answered(asked(), output("Okay.", remember={}), tools=(own,))
        self.assertEqual((state.facts, ("remember", "reachy") in kinds(rows)), ((), True))


class Repeats(unittest.TestCase):
    def talked(self, replies, device="reachy"):
        state = Companion()
        for index, reply in enumerate(replies):
            state = asked(f"line {index}", source=device, turn_id=f"{device}{index}", state=state)
            state, _ = answered(state, output(reply))
        return state

    def test_a_reply_matching_one_of_the_last_three_to_that_device_is_a_repeat(self):
        state = self.talked(["Good morning! What shall we do?", "Two plus two is four.", "Night night.", "Hello."])
        recent = policy.recent_replies(state, "reachy")
        self.assertTrue(policy.repeats(recent, "two plus two is FOUR"))
        self.assertTrue(policy.repeats(recent, "Hello"))
        self.assertFalse(policy.repeats(recent, "Good morning! What shall we do?"))  # four replies ago
        self.assertTrue(policy.repeats(recent, "Two plus two is five."))  # the same opening template
        self.assertFalse(policy.repeats(recent, "Two and two make five."))
        self.assertFalse(policy.repeats(recent, "Night night, sleep well."))  # a short reply has no template
        tail = policy.recent_replies(self.talked(["You said hi. I'm all ears."]), "reachy")
        self.assertTrue(policy.repeats(tail, "You said hello. I'm all ears."))  # the same closing template
        self.assertFalse(policy.repeats(recent, ""))  # silence is never a repeat
        self.assertEqual(policy.recent_replies(state, "mock"), ())

    def test_a_reply_that_would_unsay_what_already_streamed_is_journaled_as_what_streamed(self):
        """Live eval: "I can't see with my eyes, but I feel" streamed, then the sense fix replaced the sentence."""
        state = asked("Can you see me?")
        prompt = policy.compile_prompt(state, "reachy", MANIFEST)
        streamed = "I can't see with my eyes, but I feel"
        state, rows = policy.decided(state, "r1", prompt, output("I can't see with my eyes, but I feel your voice."), TOOLS,
                                     10, 1.0, senses=("speech",), streamed=streamed)
        self.assertEqual((rows[1].body["text"], state.exchanges[-1].reply), (streamed, streamed))
        _, rows = policy.decided(asked("Hi"), "r1", prompt, output("Hello there."), TOOLS, 10, 1.0,
                                 senses=("speech",), streamed="Hello")
        self.assertEqual(rows[1].body["text"], "Hello there.")  # a prefix: the rest is delivered

    def test_a_streaming_reply_is_held_while_it_could_still_be_a_repeat(self):
        recent = policy.recent_replies(self.talked(["Good morning, friend."]), "reachy")
        self.assertTrue(policy.may_repeat(recent, "Good "))
        self.assertTrue(policy.may_repeat(recent, "Good morning, "))
        self.assertFalse(policy.may_repeat(recent, "Good evening "))
        template = policy.recent_replies(self.talked(["I'm curious! You said hi, and so on."]), "reachy")
        self.assertTrue(policy.may_repeat(template, "I'm curious! You heard "))  # its opening is a template
        self.assertFalse(policy.may_repeat((), "Good "))

    def test_the_retry_is_the_same_prompt_plus_a_note_and_the_rejection_is_journaled(self):
        state = asked("Hi")
        prompt = policy.compile_prompt(state, "reachy", MANIFEST)
        again = policy.retry_prompt(prompt)
        self.assertEqual((again.system, again.format, again.context[:-1]), (prompt.system, prompt.format,
                                                                           prompt.context[:-1]))
        self.assertTrue(again.context[-1]["content"].startswith(prompt.context[-1]["content"]))
        self.assertEqual(policy.retry_prompt(prompt), again)  # deterministic
        rows = policy.rejected(state, "r1", prompt, output("Hello."), "repeat")
        self.assertEqual((kinds(rows), rows[0].body["reason"]), ([("rejected", None)], "repeat"))
        self.assertEqual(policy.rejected(policy.stop(state, "mock", None)[0], "r1", prompt, "", "repeat"), ())


class Senses(unittest.TestCase):
    HEARING = ("speech",)

    def test_a_claim_of_a_sense_the_body_lacks_is_caught_and_idioms_are_not(self):
        for claim in ("Yes, I can see you.", "I see you. I'm here to help.", "I'm looking at you right now.",
                      "I can see a cup on the desk.", "I saw you wave.", "I’m watching you."):
            with self.subTest(claim=claim):
                self.assertEqual(policy.false_senses(claim, self.HEARING), ["vision"])
        for honest in ("I see what you mean.", "Let me see.", "I see.", "I can't see you.", "I don't see anything.",
                       "I heard you say hello.", "I see, that sounds hard."):
            with self.subTest(honest=honest):
                self.assertEqual(policy.false_senses(honest, self.HEARING), [])
        self.assertEqual(policy.false_senses("I can see you.", ("speech", "vision")), [])
        self.assertEqual(policy.false_senses("I can hear you.", ("vision",)), ["speech"])

    def test_only_the_false_sentence_is_replaced_worded_per_missing_sense(self):
        self.assertEqual(policy.honest("Hello there. I see you. Nice day!", self.HEARING),
                         "Hello there. I can't see yet; I can only hear you. Nice day!")
        self.assertEqual(policy.honest("I can hear you fine.", ()), "I can't hear yet.")
        self.assertEqual(policy.honest("I see what you mean.", self.HEARING), "I see what you mean.")

    def test_streaming_holds_only_a_reply_that_opens_with_a_possible_claim(self):
        for held in ("I ", "I can ", "Yes, I ", "I see you "):
            with self.subTest(held=held):
                self.assertTrue(policy.may_claim(held, self.HEARING))
        for free in ("Hello ", "I heard you say ", "I see what you ", "Let me see "):
            with self.subTest(free=free):
                self.assertFalse(policy.may_claim(free, self.HEARING))
        self.assertFalse(policy.may_claim("I ", ("speech", "vision", "touch")))

    def test_the_last_resort_is_applied_when_the_answer_is_decided(self):
        state = asked("What do you see?")
        prompt = policy.compile_prompt(state, "reachy", MANIFEST)
        _, rows = policy.decided(state, "r1", prompt, output("I see you."), TOOLS, 10, 1.0, senses=self.HEARING)
        self.assertEqual(rows[1].body, {"text": "I can't see yet; I can only hear you."})


class AskedToRepeat(unittest.TestCase):
    def test_a_request_to_hear_the_last_reply_again_is_recognised(self):
        for line in ("What did you just say?", "what did you say", "Say that again.", "Can you repeat that?",
                     "What was that?", "Sorry, what?"):
            with self.subTest(line=line):
                self.assertTrue(policy.asks_to_repeat(line))
        for line in ("What do you say?", "What did I say?", "Say something.", "Repeat after me: hello."):
            with self.subTest(line=line):
                self.assertFalse(policy.asks_to_repeat(line))


class Proactive(unittest.TestCase):
    def test_presence_events_are_checked_at_the_boundary(self):
        self.assertEqual(policy.check_event({"kind": "presence", "state": "seen", "confidence": 0.82}), "presence")
        for bad in ({"kind": "presence", "state": "here", "confidence": 0.5}, {"kind": "presence", "state": "seen"},
                    {"kind": "presence", "state": "seen", "confidence": 1.5},
                    {"kind": "presence", "state": "seen", "confidence": True},
                    {"kind": "presence", "state": "lost", "confidence": 0.5, "turn_id": "x"}):
            with self.subTest(bad=bad), self.assertRaises(Refused):
                policy.check_event(bad)

    def test_a_kernel_turn_starts_for_a_device_with_no_submission_and_is_journaled_as_the_kernel_s(self):
        state, rows = policy.start_proactive(Companion(), "reachy", "kernel-1", "greet")
        self.assertEqual((state.turn.turn_id, state.turn.source, state.turn.status), ("kernel-1", "reachy", "active"))
        self.assertEqual((kinds(rows), rows[0].source, rows[0].body["reason"]), ([("proactive", None)], "kernel", "greet"))
        self.assertTrue(state.turn.text.startswith("["))  # a note to the model, never the person's words
        with self.assertRaises(Refused):
            policy.start_proactive(state, "reachy", "kernel-2", "nudge")  # never over a running turn

    def test_quiet_is_kept_in_the_checkpoint(self):
        state = replace(Companion(), quiet_until=123.0)
        self.assertEqual(policy.decode(json.loads(json.dumps(policy.encode(state)))).quiet_until, 123.0)


class Misheard(unittest.TestCase):
    def test_short_unfinished_or_repeating_lines_are_marked_as_maybe_misheard(self):
        """Live, fragments, garbles and song lyrics were answered literally; these lines are written to match."""
        for line in ("How do you And then", "Ornitholog", "Stop a strong planner.", "It's only a fork.",
                     "Ron was sure in", "Brother, brother, that's how I loved you, loved you.", "La la la la"):
            with self.subTest(line=line):
                self.assertTrue(policy.misheard(line))
        for line in ("Hi", "Hi there", "Thank you.", "Okay.", "What's my name?", "Can you hear me?",
                     "I think a softer voice would suit you.", "I didn't praise you. Honestly, you are worse than ever."):
            with self.subTest(line=line):
                self.assertFalse(policy.misheard(line))

    def test_a_maybe_misheard_line_is_framed_as_information_the_same_on_every_turn(self):
        state = asked("Ornitholog")
        framed = 'heard (may be misheard): "Ornitholog"'
        self.assertEqual(policy.compile_prompt(state, "reachy", MANIFEST).context[-1]["content"],
                         framed + policy.reminder(MANIFEST))
        state, _ = answered(state)
        later = policy.compile_prompt(asked("What's my name?", turn_id="r2", state=state), "reachy", MANIFEST)
        self.assertEqual(later.context[0]["content"], framed)  # byte-stable history
        self.assertEqual(later.context[-1]["content"], "What's my name?" + policy.reminder(MANIFEST))
        greeting, _ = policy.start_proactive(Companion(), "reachy", "kernel-1", "greet")
        self.assertTrue(policy.compile_prompt(greeting, "reachy", MANIFEST).context[-1]["content"].startswith("[The"))

    def test_the_persona_and_examples_say_what_to_do_with_a_maybe_misheard_line(self):
        self.assertIn("may be misheard", policy.PERSONA)
        self.assertIn('heard (may be misheard): "', policy.EXAMPLES)

    def test_a_greeting_uses_a_name_only_if_the_person_gave_one(self):
        """Live: the first greeting was "Hey there, Alex!" with no name ever given."""
        self.assertIn("name only if", policy.PROACTIVE["greet"])


class Persona(unittest.TestCase):
    def test_the_persona_engages_briefly_and_keeps_the_rules_that_matter(self):
        """Live run 5b: hollow replies ("Thank you.", "I'm here."), no question back, the person's name lost."""
        persona = policy.PERSONA.lower()
        for needle in ("sage", "robot", "answer what", "ask", "their name", "brief",
                       "never claim a sense your body lacks", "never say labels, tool names or emoji aloud"):
            self.assertIn(needle, persona)
        for primer in ("curious", "show feelings", "lives in", "silence is fine"):
            self.assertNotIn(primer, persona)


class Events(unittest.TestCase):
    def test_device_events_are_checked_at_the_boundary(self):
        for good in ({"kind": "submitted", "turn_id": "r1", "text": "Hi", "timing": {"stt_ms": 1}},
                     {"kind": "stop"}, {"kind": "connected"},
                     {"kind": "result", "sequence": 3, "status": "failed", "reason": "x" * 64},
                     {"kind": "result", "turn_id": "r1", "status": "interrupted", "text": "Hel"}):
            with self.subTest(good=good):
                self.assertEqual(policy.check_event(good), good["kind"])
        for bad in ({"kind": "completed"}, {"kind": "playback_started", "turn_id": "r1"}, {"kind": "dance"},
                    {"kind": "submitted", "turn_id": "r1", "text": "Hi", "source": "mock"},
                    {"kind": "connected", "colour": "red"}, {"kind": "stop", "text": "now"},
                    {"kind": "submitted", "turn_id": "r1", "text": "x" * 4001},
                    {"kind": "submitted", "turn_id": "r1", "text": " "},
                    {"kind": "submitted", "turn_id": "r" * 129, "text": "Hi"},
                    {"kind": "submitted", "turn_id": "r1", "text": "Hi", "timing": {"stt_ms": -1}},
                    {"kind": "submitted", "turn_id": "r1", "text": "Hi", "timing": {"vibes_ms": 1}},
                    {"kind": "submitted", "turn_id": "r1", "text": "Hi", "timing": {"stt_ms": True}},
                    {"kind": "result", "sequence": 3, "status": "finished"},
                    {"kind": "result", "status": "done"},
                    {"kind": "result", "sequence": 3, "turn_id": "r1", "status": "done"},
                    {"kind": "result", "sequence": 3, "status": "failed", "reason": "x" * 65},
                    {"kind": "result", "sequence": 3, "status": "failed", "reason": 7},
                    {"kind": "acknowledged"}):
            with self.subTest(bad=bad):
                with self.assertRaises(Refused) as refused:
                    policy.check_event(bad)
                self.assertEqual(refused.exception.code, "invalid")


class Limits(unittest.TestCase):
    def test_a_token_bucket_allows_a_burst_of_20_then_10_per_second(self):
        bucket, allowed = None, 0
        for _ in range(30):
            ok, bucket, retry_after = policy.take(bucket, 0.0)
            allowed += ok
        self.assertEqual(allowed, 20)
        self.assertGreater(retry_after, 0)
        self.assertTrue(policy.take(bucket, 0.1)[0])
        self.assertFalse(policy.take(policy.take(bucket, 0.1)[1], 0.1)[0])

    def test_freshness_follows_the_kernel_clock(self):
        self.assertFalse(policy.stale(100.0, 110.0, 10))
        self.assertTrue(policy.stale(100.0, 110.01, 10))


if __name__ == "__main__":
    unittest.main()
