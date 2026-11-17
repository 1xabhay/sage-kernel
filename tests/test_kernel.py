"""The kernel in process, with a scripted model: its guards (a repeat, a claimed sense), what streams, a
stopped call, proactive turns held while a body acts, memory across a restart, and the model's cache warm-up.
What a device sees over HTTP is conformance/test_conformance.py."""

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

from sage import disposition, policy
from sage.journal import Journal
from sage.kernel import Abandoned, Kernel


def answers(*replies, consumed=None):
    """A scripted model. A reply is a string, streamed in one piece, or a list of raw pieces streamed in turn.
    `consumed`, if given, gets how many pieces each call streamed before it ended."""
    queue, prompts = [r if isinstance(r, list) else [json.dumps({"reply": r})] for r in replies], []

    def decide(prompt, heard):
        prompts.append(prompt)
        pieces = queue.pop(0)
        for n, piece in enumerate(pieces):
            if consumed is not None:
                consumed[len(prompts) - 1:] = [n + 1]
            heard(piece)
        return "".join(pieces)
    return decide, prompts


VOICE = {"senses": ["speech"], "tools": []}


def until(condition):
    deadline = time.monotonic() + 5
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)


def settle(kernel):
    until(lambda: kernel.state.turn.status != "active")


def started(case, decide, manifest=VOICE, **options):
    """A kernel on a throwaway journal that has had reachy's hello (`manifest=None`: not yet)."""
    directory = tempfile.TemporaryDirectory()
    case.addCleanup(directory.cleanup)
    kernel = Kernel(Journal(Path(directory.name) / "k.sqlite3"), decide, **options)
    if manifest is not None:
        kernel.hello("reachy", {"protocols": [1], "manifest": manifest})
    return kernel


def say(kernel, turn_id, text="Hi"):
    """Reachy hears a line; what its turn delivered once it is over."""
    kernel.event("reachy", {"kind": "submitted", "turn_id": turn_id, "text": text})
    settle(kernel)
    return [e for e in kernel.effects("reachy", 0, 0, 0)["effects"] if e["turn_id"] == turn_id]


def replies(kernel, device="reachy"):
    return [e for e in kernel.effects(device, 0, 0, 0)["effects"] if e["kind"] == "reply"]


def rejected(kernel):
    """Why each answer was sent back to the model, as journaled."""
    with closing(sqlite3.connect(kernel.journal.path)) as connection:
        return [json.loads(body)["reason"] for (body,) in
                connection.execute("SELECT body FROM journal WHERE kind = 'rejected'")]


class CappedStream(unittest.TestCase):
    def test_what_streams_is_always_a_prefix_of_the_journaled_reply_when_the_cap_cuts_it(self):
        long = ("Softer voices can be fun! I'll try my best to keep things light and engaging for you. If there's "
                "anything you'd like to try with a softer voice or if you need help with something")[:160]  # format's maxLength
        streamed = []

        def decide(prompt, heard):
            output = json.dumps({"reply": long})
            for end in range(1, len(output) + 1):
                heard(output[end - 1])
                streamed.append(kernel.written.get("reachy", ""))
            return output
        kernel = started(self, decide)
        [reply] = [e["text"] for e in say(kernel, "r1", "I think a softer voice would suit you.")]
        self.assertEqual(reply, "Softer voices can be fun! I'll try my best to keep things light and engaging for you.")
        self.assertTrue(all(reply.startswith(piece.rstrip()) for piece in streamed), streamed[-1])
        self.assertEqual(kernel.state.exchanges[-1].reply, reply)  # history holds what was spoken


class AbandonedCall(unittest.TestCase):
    def test_a_stopped_turn_s_model_call_is_abandoned_so_the_next_turn_does_not_wait_behind_it(self):
        """Live: each fragment of speech stopped the running turn, but its model call ran on to the end, and
        Ollama answers one call at a time, so later turns queued behind them until they timed out at 30 s."""
        consumed, streaming, stopped = [], threading.Event(), threading.Event()  # pieces per call, in call order

        def decide(prompt, heard):
            if "Second" in prompt.context[-1]["content"]:
                return json.dumps({"reply": "About the second."})
            output = json.dumps({"reply": "About the first: " + "la " * 40})
            for n, piece in enumerate(output):
                consumed[:] = [n + 1]
                heard(piece)
                if n == 30:  # mid-answer, some of it already streamed: now the person cuts in
                    streaming.set()
                    stopped.wait(5)
            return output
        kernel = started(self, decide)
        kernel.event("reachy", {"kind": "submitted", "turn_id": "r1", "text": "First"})
        self.assertTrue(streaming.wait(5))
        kernel.event("reachy", {"kind": "stop", "turn_id": "r1"})
        stopped.set()
        self.assertEqual([e["text"] for e in say(kernel, "r2", "Second")], ["About the second."])
        self.assertEqual(consumed, [32])  # ended at the first piece after the stop, not run on to the end
        self.assertEqual(len(replies(kernel)), 1)


class AskedToRepeat(unittest.TestCase):
    def test_asked_what_it_just_said_it_may_say_it_again(self):
        decide, prompts = answers("Here's a joke about atoms.", "I said: Here's a joke about atoms.")
        kernel = started(self, decide)
        say(kernel, "r1", "Tell me a joke.")
        [again] = say(kernel, "r2", "What did you just say?")
        self.assertEqual((again["text"], len(prompts)), ("I said: Here's a joke about atoms.", 2))  # no retry, no drop


class Proactive(unittest.TestCase):
    """When a greeting may start, on a clock the test moves. That a greeting is a kernel turn, one per arrival,
    never in quiet, and what a nudge costs, are conformance tests (PROTOCOL §9)."""

    def setUp(self):
        self.now = [1000.0]
        self.decide, self.prompts = answers("Welcome back!", "Okay.", "Hello there, friend.")
        self.kernel = started(self, self.decide, {"senses": ["speech", "vision"], "tools": []},
                              clock=lambda: self.now[0],
                              timings=disposition.Timings(greet_after_s=10, nudge_after_idle_s=3600, quiet_s=3600))

    def presence(self, state, device="reachy"):
        self.kernel.event(device, {"kind": "presence", "state": state, "confidence": 0.9})

    def arrive(self, device="reachy"):
        """Away long enough to be greeted, then back."""
        self.presence("lost", device)
        self.now[0] += 11
        self.presence("seen", device)

    def tick(self):
        self.kernel.tick()
        settle(self.kernel)

    def test_no_proactive_turn_while_a_device_is_acting(self):
        [reply] = say(self.kernel, "r1")
        self.arrive()
        self.tick()  # the reply to r1 has no result yet: reachy is acting
        self.assertEqual(len(self.prompts), 1)
        self.kernel.event("reachy", {"kind": "result", "sequence": reply["sequence"], "status": "done"})
        self.tick()
        self.assertEqual(len(self.prompts), 2)
        self.assertTrue(replies(self.kernel)[-1]["turn_id"].startswith("kernel-"))

    def test_a_person_gone_again_before_the_body_was_free_is_not_greeted_late(self):
        [reply] = say(self.kernel, "r1")
        self.arrive()
        self.tick()  # held: reachy is still speaking r1
        self.presence("lost")
        self.kernel.event("reachy", {"kind": "result", "sequence": reply["sequence"], "status": "done"})
        self.tick()
        self.assertEqual(len(self.prompts), 1)  # nobody is there to greet

    def test_one_body_acting_never_holds_another_body_s_greeting(self):
        self.kernel.hello("phone", {"protocols": [1], "manifest": {"senses": [], "tools": []}})
        say(self.kernel, "r1")  # its reply has no result yet: reachy is acting
        self.arrive("phone")
        self.tick()
        [greeting] = replies(self.kernel, "phone")
        self.assertTrue(greeting["turn_id"].startswith("kernel-"))


class SenseGuard(unittest.TestCase):
    def run_turn(self, *answered, consumed=None):
        decide, prompts = answers(*answered, consumed=consumed)
        kernel = started(self, decide)
        return [e["text"] for e in say(kernel, "r1", "What do you see?")], prompts, rejected(kernel)

    def test_a_claimed_sense_is_retried_once_with_a_note(self):
        said, prompts, sent_back = self.run_turn("I see you.", "I can only hear you.")
        self.assertEqual((said, len(prompts)), (["I can only hear you."], 2))
        self.assertIn("cannot see", prompts[1].context[-1]["content"])
        self.assertEqual(sent_back, ["claims a sense this body lacks: vision"])

    def test_a_second_claim_has_only_its_false_sentence_replaced(self):
        said, _, _ = self.run_turn("I see you.", "Hello! I can see you.")
        self.assertEqual(said, ["Hello! I can't see yet; I can only hear you."])

    def test_a_claim_after_a_streamed_piece_is_repaired_with_no_second_call(self):
        """The device already plays "Hello!", and a retry's new answer could not start with it (bench: 5 of 8
        retries were thrown away for the streamed fragment). The answer is repaired instead."""
        said, prompts, sent_back = self.run_turn(['{"reply": "Hello! ', 'I can see you."}'])
        self.assertEqual((said, len(prompts), sent_back), (["Hello! I can't see yet; I can only hear you."], 1, []))

    def test_a_claim_held_back_ends_its_call_once_certain_and_retries_at_once(self):
        """Bench: a rejected decode ran to its end (~1.1 s) before the retry. "I can see" stays a claim
        whatever follows, so the call ends at the piece after it: the word "see" is then whole."""
        consumed = []
        said, prompts, sent_back = self.run_turn(
            ['{"reply": "', "I ", "can ", "see ", "you ", "and ", "your ", "red ", 'hat."}'],
            "I can only hear you.", consumed=consumed)
        self.assertEqual((said, len(prompts), sent_back),
                         (["I can only hear you."], 2, ["claims a sense this body lacks: vision"]))
        self.assertEqual(consumed[0], 5)  # ended at "you ", not run on to "hat."


class RepeatGuard(unittest.TestCase):
    def kernel(self, *answered, consumed=None):
        decide, prompts = answers(*answered, consumed=consumed)
        return started(self, decide), prompts

    def test_a_repeat_is_retried_once_with_a_note_and_the_new_reply_is_delivered(self):
        kernel, prompts = self.kernel("Hello there.", "Hello there!", "Nice to see you.")
        say(kernel, "r1")
        self.assertEqual([e["text"] for e in say(kernel, "r2")], ["Nice to see you."])
        self.assertEqual(len(prompts), 3)
        self.assertEqual(prompts[2].context[:-1], prompts[1].context[:-1])
        self.assertIn('"Hello there!"', prompts[2].context[-1]["content"])  # the note quotes the repeat
        self.assertEqual(rejected(kernel), ["repeat of a recent reply"])

    def test_a_repeat_after_a_streamed_piece_is_delivered_whole_with_no_second_call(self):
        kernel, prompts = self.kernel("It is good to hear you.", ['{"reply": "Oh wow! ', 'It is good to hear you."}'])
        say(kernel, "r1")
        self.assertEqual([e["text"] for e in say(kernel, "r2")], ["Oh wow! It is good to hear you."])
        self.assertEqual((len(prompts), rejected(kernel)), (2, []))

    def test_a_template_repeat_held_back_ends_its_call_after_four_words_and_retries_at_once(self):
        consumed = []
        kernel, prompts = self.kernel(
            "It is good to hear you.",
            ['{"reply": "', "It ", "is ", "good ", "to ", "hear ", "from ", "you ", 'again."}'],
            "Nice to see you.", consumed=consumed)
        say(kernel, "r1")
        self.assertEqual([e["text"] for e in say(kernel, "r2")], ["Nice to see you."])
        self.assertEqual((len(prompts), rejected(kernel)), (3, ["repeat of a recent reply"]))
        self.assertEqual(consumed[1], 6)  # ended once "to" was whole, not run on to "again."

    def test_a_second_repeat_is_delivered_anyway(self):
        kernel, prompts = self.kernel("Hello there.", "Hello there.", "hello, there")
        say(kernel, "r1")
        self.assertEqual([e["text"] for e in say(kernel, "r2")], ["hello, there"])
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
                kernel.hello("reachy", {"protocols": [1], "manifest": VOICE})
            say(kernel, f"r{n}", text)
        self.assertIn("- Their name is Juno.", prompts[-1].system)
        self.assertNotIn("Juno", json.dumps(prompts[-1].context))
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT body, target FROM journal WHERE kind = 'remember'").fetchall(),
                             [('{"fact": "Their name is Juno."}', None)])


class Prewarm(unittest.TestCase):
    """A cold prompt prefill cost ~6 s on the first turn after a restart and after a body change: a changed
    manifest warms the model's prompt cache in the background, so the next real turn starts warm."""

    def setUp(self):
        self.warmed, self.release, self.outcomes = [], threading.Event(), []
        self.release.set()

        def warm(prompt, heard):
            self.warmed.append(prompt)
            self.release.wait(5)  # the prefill
            try:
                heard("{")
                self.outcomes.append("warm")
            except Abandoned:
                self.outcomes.append("abandoned")
                raise
            return "{"
        self.decide, self.prompts = answers("Hello!", "Again!")
        self.kernel = started(self, self.decide, manifest=None, warm=warm)

    def test_each_changed_manifest_warms_once_with_the_next_turn_s_prefix_and_journals_nothing(self):
        kernel, head = self.kernel, self.kernel.journal.head
        body = {**VOICE, "body": "A small robot."}
        for manifest, n in ((VOICE, 1), (VOICE, 1), (body, 2), (body, 2)):
            kernel.hello("reachy", {"protocols": [1], "manifest": manifest})
            until(lambda n=n: len(self.outcomes) >= n)
        time.sleep(0.05)
        self.assertEqual((len(self.warmed), self.outcomes), (2, ["warm", "warm"]))
        self.assertEqual(kernel.journal.head, head)
        say(kernel, "r1")
        self.assertEqual(self.prompts[0].system, self.warmed[-1].system)
        self.assertEqual(self.prompts[0].format, self.warmed[-1].format)

    def test_no_warm_starts_during_a_turn_and_a_turn_that_starts_abandons_it_without_waiting(self):
        kernel, gate = self.kernel, threading.Event()

        def slow(prompt, heard):
            gate.wait(5)
            return self.decide(prompt, heard)
        kernel.decide = slow
        kernel.hello("reachy", {"protocols": [1], "manifest": VOICE})
        until(lambda: len(self.outcomes) == 1)
        kernel.event("reachy", {"kind": "submitted", "turn_id": "r1", "text": "Hi"})
        kernel.hello("reachy", {"protocols": [1], "manifest": {**VOICE, "body": "A small robot."}})  # mid-turn
        gate.set()
        settle(kernel)
        time.sleep(0.05)
        until(lambda: len(self.outcomes) == 2)
        self.assertEqual(len(self.warmed), 2)  # the first hello's and the one after the reply, not the mid-turn hello's

        self.release.clear()  # the next warm is still prefilling when a turn arrives
        kernel.hello("reachy", {"protocols": [1], "manifest": {**VOICE, "body": "A tall robot."}})
        until(lambda: len(self.warmed) == 3)
        gate.clear()
        kernel.event("reachy", {"kind": "submitted", "turn_id": "r2", "text": "Hi again"})
        self.release.set()
        until(lambda: len(self.outcomes) == 3)
        self.assertEqual(self.outcomes[-1], "abandoned")
        gate.set()
        settle(kernel)
        self.assertEqual(kernel.state.turn.status, "completed")

    def test_a_reply_and_a_trim_prefill_the_next_turn_s_prefix_and_a_turn_mid_prefill_is_served(self):
        """The next turn's prefix (this exchange, a trim, a new fact) is prefilled while the body speaks:
        ~150 ms on every turn, ~600 ms after a trim."""
        kernel = self.kernel
        kernel.decide, self.prompts = answers(*(f"Reply {i}." for i in range(policy.WINDOW + 2)))
        kernel.hello("reachy", {"protocols": [1], "manifest": VOICE})
        until(lambda: len(self.outcomes) == 1)
        for index in range(policy.WINDOW + 1):  # the last one trims the window
            say(kernel, f"r{index}", f"Line {index}.")
            until(lambda n=index + 2: len(self.outcomes) == n)
            nxt = policy.compile_prompt(kernel.state, "reachy", VOICE)
            self.assertEqual(self.warmed[-1].messages()[:-1], nxt.messages()[:-1])
        self.assertEqual(len(kernel.state.exchanges), policy.KEPT)

        self.release.clear()  # a turn arrives while the next prefill still runs
        say(kernel, "late", "Still there?")
        self.assertEqual(kernel.state.turn.status, "completed")
        when, line = self.prompts[-1].context[-1]["content"].split("\n", 1)  # [when it was heard], then the line
        self.assertTrue(when.startswith("[") and line.startswith("Still there?"), (when, line))
        self.release.set()

    def test_a_real_turn_right_after_a_hello_does_not_wait_for_the_warm(self):
        kernel = self.kernel
        self.release.clear()
        kernel.hello("reachy", {"protocols": [1], "manifest": VOICE})
        until(lambda: len(self.warmed) == 1)
        say(kernel, "r1")
        self.assertEqual(kernel.state.turn.status, "completed")  # while the warm still holds
        self.release.set()


if __name__ == "__main__":
    unittest.main()
