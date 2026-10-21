"""The kernel's repeat guard, end to end with a scripted model: retry once, never go silent."""

import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from sage import disposition, policy
from sage.journal import Journal
from sage.kernel import Kernel


def answers(*replies):
    queue, prompts = [json.dumps({"reply": reply}) for reply in replies], []

    def decide(prompt, heard):
        prompts.append(prompt)
        output = queue.pop(0)
        heard(output)
        return output
    return decide, prompts


EMOTE = {"name": "emote", "description": "Show a feeling.", "max_s": 3,
         "inputSchema": {"type": "object", "properties": {"name": {"enum": ["sad1"]}}}}


class DeviceResults(unittest.TestCase):
    def test_a_real_done_is_never_reported_and_a_failure_names_the_body_once(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        prompts, queue = [], [json.dumps({"reply": f"Answer {n}.", "emote": {"name": "sad1"}}) for n in range(3)]

        def decide(prompt, heard):
            prompts.append(prompt)
            return queue.pop(0)
        kernel = Kernel(Journal(Path(directory.name) / "k.sqlite3"), decide)
        kernel.hello("reachy", {"protocols": [1], "manifest": {"senses": ["speech"], "tools": [EMOTE]}})
        for turn_id, status, reason in (("r1", "done", None), ("r2", "failed", "motor_hot"), ("r3", None, None)):
            kernel.event("reachy", {"kind": "submitted", "turn_id": turn_id, "text": f"Line {turn_id}"})
            deadline = time.monotonic() + 5
            while kernel.state.turn.status == "active" and time.monotonic() < deadline:
                time.sleep(0.01)
            if status:
                emote = next(e for e in kernel.effects("reachy", 0, 0, 0)["effects"]
                             if e["kind"] == "emote" and e["turn_id"] == turn_id)
                kernel.event("reachy", {"kind": "result", "sequence": emote["sequence"], "status": status,
                                        **({"reason": reason} if reason else {})})
        tools = [m["content"] for m in prompts[-1].context if m["role"] == "tool"]
        self.assertEqual(tools, ["(your reachy body could not do emote: motor_hot)"])


class CappedStream(unittest.TestCase):
    def test_what_streams_is_always_a_prefix_of_the_journaled_reply_when_the_cap_cuts_it(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        long = ("Softer voices can be fun! I'll try my best to keep things light and engaging for you. If there's "
                "anything you'd like to try with a softer voice or if you need help with something")[:160]  # format's maxLength
        streamed = []

        def decide(prompt, heard):
            output = json.dumps({"reply": long})
            for end in range(1, len(output) + 1):
                heard(output[end - 1])
                streamed.append(kernel.written)
            return output
        kernel = Kernel(Journal(Path(directory.name) / "k.sqlite3"), decide)
        kernel.hello("reachy", {"protocols": [1], "manifest": {"senses": ["speech"], "tools": []}})
        kernel.event("reachy", {"kind": "submitted", "turn_id": "r1", "text": "I think a softer voice would suit you."})
        deadline = time.monotonic() + 5
        while kernel.state.turn.status == "active" and time.monotonic() < deadline:
            time.sleep(0.01)
        reply = next(e["text"] for e in kernel.effects("reachy", 0, 0, 0)["effects"] if e["kind"] == "reply")
        self.assertEqual(reply, "Softer voices can be fun! I'll try my best to keep things light and engaging for you.")
        self.assertTrue(all(reply.startswith(piece.rstrip()) for piece in streamed), streamed[-1])
        self.assertEqual(kernel.state.exchanges[-1].reply, reply)  # history holds what was spoken


class AskedToRepeat(unittest.TestCase):
    def test_asked_what_it_just_said_it_may_say_it_again(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        decide, prompts = answers("Here's a joke about atoms.", "I said: Here's a joke about atoms.")
        kernel = Kernel(Journal(Path(directory.name) / "k.sqlite3"), decide)
        kernel.hello("reachy", {"protocols": [1], "manifest": {"senses": ["speech"], "tools": []}})
        for turn_id, text in (("r1", "Tell me a joke."), ("r2", "What did you just say?")):
            kernel.event("reachy", {"kind": "submitted", "turn_id": turn_id, "text": text})
            deadline = time.monotonic() + 5
            while kernel.state.turn.status == "active" and time.monotonic() < deadline:
                time.sleep(0.01)
        replies = [e["text"] for e in kernel.effects("reachy", 0, 0, 0)["effects"] if e["kind"] == "reply"]
        self.assertEqual((replies[-1], len(prompts)), ("I said: Here's a joke about atoms.", 2))  # no retry, no drop


class Proactive(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = [1000.0]
        self.decide, self.prompts = answers("Welcome back!", "Okay.", "Hello there, friend.")
        self.kernel = Kernel(Journal(Path(directory.name) / "k.sqlite3"), self.decide, clock=lambda: self.now[0],
                             timings=disposition.Timings(greet_after_s=10, nudge_after_idle_s=3600, quiet_s=3600))
        self.kernel.hello("reachy", {"protocols": [1], "manifest": {"senses": ["speech", "vision"], "tools": []}})

    def presence(self, state, confidence=0.9, device="reachy"):
        self.kernel.event(device, {"kind": "presence", "state": state, "confidence": confidence})

    def settle(self):
        deadline = time.monotonic() + 5
        while self.kernel.state.turn.status == "active" and time.monotonic() < deadline:
            time.sleep(0.01)

    def replies(self):
        return [e for e in self.kernel.effects("reachy", 0, 0, 0)["effects"] if e["kind"] == "reply"]

    def test_an_arrival_is_greeted_by_a_kernel_turn_once_the_body_is_free(self):
        self.presence("lost")
        self.now[0] += 11
        self.presence("seen")
        self.assertEqual(self.prompts, [])  # presence never calls the model by itself
        self.kernel.tick()
        self.settle()
        [greeting] = self.replies()
        self.assertTrue(greeting["turn_id"].startswith("kernel-"))
        self.kernel.tick()
        self.settle()
        self.assertEqual(len(self.prompts), 1)  # one greeting per arrival

    def test_a_low_confidence_or_repeated_seen_is_no_arrival(self):
        self.presence("lost")
        self.now[0] += 11
        self.presence("seen", confidence=0.4)
        self.kernel.tick()
        self.assertEqual(self.prompts, [])

    def test_no_proactive_turn_while_a_device_is_acting(self):
        self.kernel.event("reachy", {"kind": "submitted", "turn_id": "r1", "text": "Hi"})
        self.settle()
        self.presence("lost")
        self.now[0] += 11
        self.presence("seen")
        self.kernel.tick()  # the reply to r1 has no result yet: reachy is acting
        self.assertEqual(len(self.prompts), 1)
        reply = self.replies()[0]
        self.kernel.event("reachy", {"kind": "result", "sequence": reply["sequence"], "status": "done"})
        self.kernel.tick()
        self.settle()
        self.assertEqual(len(self.prompts), 2)

    def test_a_nudge_answered_with_silence_still_counts_against_the_budget_and_gap(self):
        kernel = Kernel(Journal(self.kernel.journal.path.with_name("n.sqlite3")), answers("", "", "")[0],
                        clock=lambda: self.now[0],
                        timings=disposition.Timings(greet_on_start=False, nudge_after_idle_s=1, nudge_gap_s=100))
        kernel.event("reachy", {"kind": "presence", "state": "seen", "confidence": 0.9})
        self.now[0] += 2
        kernel.tick()
        deadline = time.monotonic() + 5
        while kernel.state.turn.status == "active" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(kernel.effects("reachy", 0, 0, 0)["effects"], [])  # silence delivers nothing
        self.now[0] += 2
        kernel.tick()  # within the gap: no second nudge
        self.assertEqual(kernel.state.turn.status, "completed")
        self.assertEqual(len(kernel.disposition.nudges), 1)

    def test_quiet_survives_a_restart(self):
        self.kernel.event("reachy", {"kind": "submitted", "turn_id": "r1", "text": "Be quiet."})
        self.settle()
        again = Kernel(self.kernel.journal.__class__(self.kernel.journal.path), self.decide,
                       clock=lambda: self.now[0], timings=disposition.Timings(10, 3600, 3600))
        self.assertEqual(disposition.state(again.disposition, self.now[0] + 1, again.timings), "quiet")


class SenseGuard(unittest.TestCase):
    def run_turn(self, *replies):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        decide, prompts = answers(*replies)
        kernel = Kernel(Journal(Path(directory.name) / "k.sqlite3"), decide)
        kernel.hello("reachy", {"protocols": [1], "manifest": {"senses": ["speech"], "tools": []}})
        kernel.event("reachy", {"kind": "submitted", "turn_id": "r1", "text": "What do you see?"})
        deadline = time.monotonic() + 5
        while kernel.state.turn.status == "active" and time.monotonic() < deadline:
            time.sleep(0.01)
        with closing(sqlite3.connect(kernel.journal.path)) as connection:
            rejected = [json.loads(b)["reason"] for (b,) in connection.execute(
                "SELECT body FROM journal WHERE kind = 'rejected'")]
        return [e["text"] for e in kernel.effects("reachy", 0, 0, 0)["effects"] if e["kind"] == "reply"], prompts, rejected

    def test_a_claimed_sense_is_retried_once_with_a_note(self):
        replies, prompts, rejected = self.run_turn("I see you.", "I can only hear you.")
        self.assertEqual((replies, len(prompts)), (["I can only hear you."], 2))
        self.assertIn("cannot see", prompts[1].context[-1]["content"])
        self.assertEqual(rejected, ["claims a sense this body lacks: vision"])

    def test_a_second_claim_has_only_its_false_sentence_replaced(self):
        replies, _, _ = self.run_turn("I see you.", "Hello! I can see you.")
        self.assertEqual(replies, ["Hello! I can't see yet; I can only hear you."])


class RepeatGuard(unittest.TestCase):
    def kernel(self, *replies):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        decide, prompts = answers(*replies)
        return Kernel(Journal(Path(directory.name) / "k.sqlite3"), decide), prompts

    def turn(self, kernel, turn_id, text="Hi"):
        kernel.event("reachy", {"kind": "submitted", "turn_id": turn_id, "text": text})
        deadline = time.monotonic() + 5
        while kernel.state.turn.status == "active" and time.monotonic() < deadline:
            time.sleep(0.01)
        return [e for e in kernel.effects("reachy", 0, 0, 0)["effects"] if e["turn_id"] == turn_id]

    def rejected(self, kernel):
        with closing(sqlite3.connect(kernel.journal.path)) as connection:
            return [json.loads(body)["reason"] for (body,) in
                    connection.execute("SELECT body FROM journal WHERE kind = 'rejected'")]

    def test_a_repeat_is_retried_once_with_a_note_and_the_new_reply_is_delivered(self):
        kernel, prompts = self.kernel("Hello there.", "Hello there!", "Nice to see you.")
        self.turn(kernel, "r1")
        self.assertEqual([e["text"] for e in self.turn(kernel, "r2")], ["Nice to see you."])
        self.assertEqual(len(prompts), 3)
        self.assertEqual(prompts[2].context[:-1], prompts[1].context[:-1])
        self.assertIn('"Hello there!"', prompts[2].context[-1]["content"])  # the note quotes the repeat
        self.assertEqual(self.rejected(kernel), ["repeat of a recent reply"])

    def test_a_second_repeat_is_delivered_anyway(self):
        kernel, prompts = self.kernel("Hello there.", "Hello there.", "hello, there")
        self.turn(kernel, "r1")
        self.assertEqual([e["text"] for e in self.turn(kernel, "r2")], ["hello, there"])
        self.assertEqual(len(prompts), 3)


class Remembering(unittest.TestCase):
    def test_a_name_outlives_the_window_and_a_restart(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path, prompts = Path(directory.name) / "k.sqlite3", []
        queue = [json.dumps({"reply": "Hi Juno!", "remember": "Their name is Juno."})]
        lines = policy.WINDOW + 1  # enough that the window has dropped the first exchange
        queue += [json.dumps({"reply": f"Reply number {n}."}) for n in range(lines)]

        def decide(prompt, heard):
            prompts.append(prompt)
            return queue.pop(0)
        for n, text in enumerate(["My name is Juno."] + [f"Line {n}." for n in range(lines)]):
            if n in (0, lines):  # the last exchange after a restart
                kernel = Kernel(Journal(path), decide)
                kernel.hello("reachy", {"protocols": [1], "manifest": {"senses": ["speech"], "tools": []}})
            kernel.event("reachy", {"kind": "submitted", "turn_id": f"r{n}", "text": text})
            deadline = time.monotonic() + 5
            while kernel.state.turn.status == "active" and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertIn("- Their name is Juno.", prompts[-1].system)
        self.assertNotIn("Juno", json.dumps(prompts[-1].context))
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT body, target FROM journal WHERE kind = 'remember'").fetchall(),
                             [('{"fact": "Their name is Juno."}', None)])


if __name__ == "__main__":
    unittest.main()
