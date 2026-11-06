"""How the prompt renders per device: one body's history in full, other bodies' as narrated lines, and the
device's output medium (speech or text)."""

import json
import os
import unittest
from dataclasses import replace
from pathlib import Path

from sage import manifest, policy
from sage.policy import Companion

HERE = Path(__file__).parent
GOLDEN = HERE / "golden_prompt.json"
VOICE = manifest.check_manifest(json.loads((HERE / "voice_manifest.json").read_text()))
PLAN = {"act_id": "a-1", "origin": "reflex", "what": "body_plan", "input": {"steps": [{"skill": "search_face"}]},
        "status": "failed", "reason": "not_found", "said": "Where did you go?"}


def turn(state, device, turn_id, text, answer, input=None, now=1.0):
    state = policy.submit(state, device, turn_id, text, {}, False, input)[0]
    prompt = policy.compile_prompt(state, device, VOICE)
    return policy.decided(state, turn_id, prompt, json.dumps(answer), VOICE["tools"], 10 * len(state.exchanges), now)[0]


def one_body():
    """A single device's session touching every part of the prompt: memory, a tool call that failed and one
    that finished, an interrupted reply, a maybe-misheard line, an act, notes and a proactive turn."""
    state = turn(Companion(), "reachy", "r1", "My name's Mika.", {"reply": "Hi Mika!", "remember": "Their name is Mika."})
    state = turn(state, "reachy", "r2", "Show me happy.", {"reply": "Like this!", "emote": {"name": "cheerful1"}})
    effect = next(o for o in state.open if o.kind == "emote")
    state = policy.report(state, "reachy", {"kind": "emote", "target": "reachy", "sequence": effect.sequence},
                          "done", "smiling", None, {})[0]
    state = turn(state, "reachy", "r3", "fridge banana wire", {"reply": "Huh? The fridge?", "emote": {"name": "nope"}})
    state = turn(state, "reachy", "r4", "Tell me a long story.", {"reply": "Once upon a time there was a robot."})
    state = policy.report_turn(state, "reachy", "r4", {"kind": "reply", "target": "reachy",
                                                       "sequence": state.exchanges[-1].reply_sequence},
                               "interrupted", "Once upon", None, {})[0]
    state = policy.act(state, "reachy", PLAN)[0]
    state = policy.take_notes(state, "reachy", "Pulse", ("Pulse: a 2026 single by Mira Vale.", "Produced by Odo."))[0]
    state = turn(state, "reachy", "r5", "Who produced Pulse?", {"reply": "Odo did."}, {"medium": "speech", "confidence": 0.5})
    state = policy.start_proactive(state, "reachy", "k1", "greet")[0]
    return state


def rendered(prompt: policy.CompiledPrompt) -> dict:
    return {"messages": prompt.messages(), "format": prompt.format}


class SingleDevice(unittest.TestCase):
    def test_one_device_s_prompt_is_byte_for_byte_the_golden_one(self):
        """The prompt v1.1 sent for one body; per-device rendering must not change a byte of it (cache, evals).
        SAGE_WRITE_GOLDEN=1 rewrites the file: only for a change meant to alter every prompt."""
        got = json.dumps(rendered(policy.compile_prompt(one_body(), "reachy", VOICE)), indent=1, ensure_ascii=False) + "\n"
        if os.environ.get("SAGE_WRITE_GOLDEN"):
            GOLDEN.write_text(got)
        self.assertEqual(got, GOLDEN.read_text())


class CachedPrefix(unittest.TestCase):
    def test_the_next_prompt_starts_with_this_request_and_the_answer_as_the_model_streamed_it(self):
        """The model server re-reads all after the first changed byte: the last user line keeps its reminder,
        and the answer is the model's own bytes, not re-serialized (\\u2014 against —: ~150 ms a turn)."""
        state = turn(Companion(), "reachy", "r1", "Hi.", {"reply": "Hi!"})
        state = policy.submit(state, "reachy", "r2", "Got two minutes?", {}, False)[0]
        sent = policy.compile_prompt(state, "reachy", VOICE)
        output = '{"reply": "Got it! Two minutes \\u2014 let’s go!", "emote": {"name": "cheerful1"}}'
        state = policy.decided(state, "r2", sent, output, VOICE["tools"], 20, 1.0)[0]
        state = policy.submit(state, "reachy", "r3", "Great.", {}, False)[0]
        expected = sent.messages() + [{"role": "assistant", "content": output}]
        self.assertEqual(policy.compile_prompt(state, "reachy", VOICE).messages()[:len(expected)], expected)


class OtherBodies(unittest.TestCase):
    """One identity across bodies: what was said on another body is told as one narrated line, never as that
    body's answer JSON, tool calls or reports, which the model would echo as its own (design §10 row 5)."""

    def test_another_body_s_exchanges_are_one_narrated_line_and_its_tools_never_show(self):
        state = turn(Companion(), "phone", "p1", "I'm at the station.", {"reply": "Safe trip!", "emote": {"name": "nope"}},
                     {"medium": "text"})
        state = policy.act(state, "phone", PLAN)[0]
        state = turn(state, "reachy", "r1", "I'm home now, it was a long ride.", {"reply": "Welcome back!", "emote": {"name": "cheerful1"}})
        state = policy.submit(state, "reachy", "r2", "Did you miss me?", {}, False)[0]
        context = policy.compile_prompt(state, "reachy", VOICE).context
        self.assertEqual(context[0], {"role": "tool", "content": '(on phone) they said "I\'m at the station."; '
                                                                 'you said "Safe trip!"'})
        self.assertNotIn("nope", json.dumps(context))  # the phone's tool call and its failure
        self.assertNotIn("search_face", json.dumps(context))  # the phone's own act
        self.assertEqual(context[1:3], ({"role": "user", "content": "I'm home now, it was a long ride." + policy.reminder(VOICE)},
                                        {"role": "assistant", "content": json.dumps(
                                            {"reply": "Welcome back!", "emote": {"name": "cheerful1"}})}))
        phone = policy.compile_prompt(replace(state, turn=replace(state.turn, source="phone")), "phone", VOICE).context
        self.assertEqual(phone[-3]["content"], '(on reachy) they said "I\'m home now, it was a long ride."; you said "Welcome back!"')
        self.assertIn("search_face", phone[-2]["content"])  # the phone's own act waits for the phone's next line

    def test_a_kernel_note_from_another_body_is_told_as_what_you_said(self):
        state = policy.start_proactive(Companion(), "reachy", "k1", "greet")[0]
        state = policy.decided(state, "k1", policy.compile_prompt(state, "reachy", VOICE), json.dumps({"reply": "Hey!"}),
                               VOICE["tools"], 1, 1.0)[0]
        state = policy.submit(state, "phone", "p1", "Hi", {}, False)[0]
        self.assertEqual(policy.compile_prompt(state, "phone", VOICE).context[0]["content"], '(on reachy) you said "Hey!"')


TYPED = manifest.check_manifest({"senses": [], "tools": [], "output": {"medium": "text", "max_chars": 500}})


class Output(unittest.TestCase):
    """The manifest's optional output: how the body puts out a reply. Absent, it is speech of 160 characters."""

    def test_output_is_checked_and_kept_only_when_declared(self):
        self.assertEqual(TYPED["output"], {"medium": "text", "max_chars": 500})
        self.assertNotIn("output", manifest.check_manifest({"senses": [], "tools": []}))
        self.assertEqual(manifest.check_manifest({"output": {"medium": "speech"}})["output"], {"medium": "speech"})
        for bad in ([], {"medium": "video"}, {"max_chars": 100}, {"medium": "text", "max_chars": 0},
                    {"medium": "text", "max_chars": 4001}, {"medium": "text", "max_chars": True},
                    {"medium": "text", "max_chars": 9.5}, {"medium": "text", "voice": "low"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                manifest.check_manifest({"output": bad})

    def test_the_reply_is_constrained_and_cut_to_the_body_s_max_chars(self):
        cut = ("This is a sentence of about forty chars. " * 20)[:500]  # as the decoder stops it, mid-sentence
        for declared, cap in ((TYPED, 500), ({"senses": (), "tools": ()}, manifest.MAX_REPLY)):
            with self.subTest(cap=cap):
                state = policy.submit(Companion(), "phone", "p1", "Tell me everything.", {}, False)[0]
                prompt = policy.compile_prompt(state, "phone", declared)
                self.assertEqual(prompt.format["properties"]["reply"]["maxLength"], cap)
                state = policy.decided(state, "p1", prompt, json.dumps({"reply": cut}), (), 1, 1.0)[0]
                self.assertGreater(len(state.exchanges[-1].reply), cap - 45)
                self.assertLessEqual(len(state.exchanges[-1].reply), cap)


class TextMedium(unittest.TestCase):
    """A text body is never misheard and stands in front of no one: the speech and robot wording goes."""
    SPOKEN = ("misheard", "spoken", "aloud", "in front of you", "robot", "into view")

    def test_a_text_body_s_prompt_has_no_speech_or_robot_wording(self):
        state = turn(Companion(), "phone", "p1", "Look left.", {"reply": "Which way is left for a phone?"},
                     {"medium": "speech", "confidence": 0.2})
        state = policy.start_proactive(state, "phone", "k1", "greet")[0]
        prompt = policy.compile_prompt(state, "phone", TYPED)
        text = "\n".join(m["content"] for m in prompt.messages()).lower()
        for wording in self.SPOKEN:
            with self.subTest(wording=wording):
                self.assertNotIn(wording, text)
        self.assertTrue(prompt.system.startswith("You are Sage"))
        self.assertEqual(prompt.context[0]["content"], "Look left." + policy.reminder(TYPED))
        self.assertIn("greet them", prompt.context[-1]["content"].lower())

    def test_a_declared_speech_body_s_prompt_is_the_same_as_an_undeclared_one(self):
        state = policy.submit(Companion(), "reachy", "r1", "Hi", {}, False)[0]
        spoken = manifest.check_manifest({**json.loads((HERE / "voice_manifest.json").read_text()),
                                          "output": {"medium": "speech", "max_chars": manifest.MAX_REPLY}})
        plain = {key: value for key, value in VOICE.items() if key != "output"}
        self.assertEqual(policy.compile_prompt(state, "reachy", spoken), policy.compile_prompt(state, "reachy", plain))


if __name__ == "__main__":
    unittest.main()
