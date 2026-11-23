"""Memory, written after the conversation (design 3-memory, 2026-11-21): a conversation ends, a writer reads the
lines that were said to Sage, the kernel keeps only what cites one of them, and the next conversation starts from
a short block of what is remembered. The writer is a model call; here it is always a scripted one. Every line is
invented, and every journal is a throwaway one."""

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sage import manifest, policy
from sage.__main__ import memory
from sage.adapters import model
from sage.journal import Journal
from sage.kernel import Abandoned, Kernel
from sage.policy import Companion

ROOT = Path(__file__).resolve().parents[1]
VOICE = {"senses": ["speech"], "tools": []}
BODY = {"senses": ("speech",), "tools": ()}
SURE = {"medium": "speech", "confidence": 0.95}


def record(id, text, who="owner", kind="person", at=1.0, source=None):
    return {"id": id, "who": who, "kind": kind, "text": text, "at": at, "from": source}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def until(condition):
    deadline = time.monotonic() + 5
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


def cited(prompt, text) -> int:
    """The number the writer's prompt gives the line `text`."""
    return int(re.search(r"\[(\d+)\] " + re.escape(text), prompt.context[-1]["content"])[1])


class Home(unittest.TestCase):
    """A kernel on a throwaway journal, a clock the test moves, a scripted model and a scripted writer."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.clock, self.asked, self.wrote, self.n = Clock(), [], [], 0
        self.writes = lambda prompt: {"operations": [], "day": ""}  # what the writer answers; a test sets its own

    def kernel(self, **options):
        def decide(prompt, heard):
            self.asked.append(prompt)
            return json.dumps({"reply": f"Reply {len(self.asked)}."})

        def write(prompt, heard):
            self.wrote.append(prompt)
            answer = json.dumps(self.writes(prompt))
            heard(answer)
            return answer
        kernel = Kernel(Journal(self.dir / "kernel.sqlite3"), decide, clock=self.clock, **{"write": write, **options})
        kernel.hello("reachy", {"protocols": [1], "manifest": VOICE})
        return kernel

    def say(self, kernel, text, speaker="owner", addressed=True, input=SURE):
        """Reachy hears `text`; its turn's reply, once the turn is over."""
        self.n += 1
        kernel.buckets.clear()  # forty lines in a second are a test's, not a flood
        kernel.event("reachy", {"kind": "submitted", "turn_id": f"t{self.n}", "text": text, "input": input},
                     addressed=addressed, speaker=speaker)
        until(lambda: policy.lane(kernel.state, "reachy").status != "active")
        return next((e["text"] for e in kernel.effects("reachy", 0, 0, 0)["effects"]
                     if e["kind"] == "reply" and e["turn_id"] == f"t{self.n}"), "")

    def later(self, kernel, seconds=policy.SESSION_IDLE_S + 1):
        """`seconds` pass with nothing said, and the kernel's own clock ticks once; the writer, if owed, is done."""
        self.clock.now += seconds
        kernel.tick()
        until(lambda: not kernel.writing)

    def rows(self, kernel, kind):
        with closing(sqlite3.connect(kernel.journal.path)) as connection:
            return [(source, json.loads(body)) for source, body in connection.execute(
                "SELECT source, body FROM journal WHERE kind = ? ORDER BY sequence", (kind,))]

    def texts(self, kernel):
        return [r["text"] for r in kernel.state.facts]

    def sequence(self, kernel, text) -> int:
        with closing(sqlite3.connect(kernel.journal.path)) as connection:
            return next(sequence for sequence, body in connection.execute(
                "SELECT sequence, body FROM journal WHERE kind = 'submitted'") if json.loads(body)["text"] == text)


class NotTheSmallBrainsJob(unittest.TestCase):
    def test_remember_and_forget_are_gone_from_the_answer_s_schema_the_prompt_and_the_examples(self):
        self.assertEqual(list(manifest.format_schema(())["properties"]), ["reply"])
        state = policy.submit(Companion(), "reachy", "r1", "My name is Wren.", {}, False)[0]
        prompt = policy.compile_prompt(state, "reachy", BODY)
        self.assertNotIn('"remember"', prompt.system)
        self.assertNotIn('"forget"', prompt.system)

    def test_a_model_that_still_answers_remember_writes_nothing(self):
        state = policy.submit(Companion(), "reachy", "r1", "My name is Wren.", {}, False)[0]
        prompt = policy.compile_prompt(state, "reachy", BODY)
        state, rows = policy.decided(state, "r1", prompt, json.dumps({"reply": "Hi Wren!", "remember": "Wren."}), (), 2, 1.0)
        self.assertEqual(state.facts, ())
        self.assertNotIn("remember", [row.kind for row in rows])


# A checkpoint as a kernel before records wrote it: a fact was its text.
OLD = {"turn": {"turn_id": "r1", "source": "reachy", "status": "completed", "text": "", "timing": {}}, "open": [],
       "exchanges": [], "facts": ["Their name is Juno.", "Likes oolong tea."]}


class OldCheckpoints(unittest.TestCase):

    def test_the_facts_of_an_earlier_kernel_load_as_records_every_one_and_stay_in_the_prompt(self):
        state = policy.decode(OLD)
        self.assertEqual([(r["id"], r["text"], r["from"]) for r in state.facts],
                         [(1, "Their name is Juno.", None), (2, "Likes oolong tea.", None)])
        self.assertTrue(all(r["who"] == "unknown" for r in state.facts))  # nobody said whose voice it was
        self.assertEqual(policy.decode(json.loads(json.dumps(policy.encode(state)))), state)
        system = policy.compile_prompt(policy.focus(state, "reachy"), "reachy", BODY).system
        self.assertIn("- Their name is Juno.", system)
        self.assertIn("- Likes oolong tea.", system)

    def test_an_earlier_journal_opens_and_its_facts_survive_the_kernel_s_next_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.sqlite3"
            Journal(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("INSERT INTO checkpoint VALUES (1, ?)", (json.dumps(OLD),))
            kernel = Kernel(Journal(path), lambda prompt, heard: "{}")  # its start commits a row
            self.assertEqual([r["text"] for r in Journal(path).state.facts], OLD["facts"])
            self.assertEqual(kernel.state.facts, Journal(path).state.facts)


class Conversations(Home):
    def test_five_minutes_with_nothing_said_to_it_ends_the_conversation_and_empties_the_window(self):
        kernel = self.kernel()
        self.say(kernel, "My sister visits on Friday.")
        self.say(kernel, "She is bringing the dog.")
        self.later(kernel, policy.SESSION_IDLE_S - 1)
        self.assertEqual(len(kernel.state.exchanges), 2)  # not yet
        self.later(kernel, 2)
        self.assertEqual(kernel.state.exchanges, ())
        self.assertEqual(self.rows(kernel, "session"), [("kernel", {"turns": 2, "reason": "idle"})])
        self.say(kernel, "Good morning.")
        self.assertNotIn("sister", json.dumps(self.asked[-1].context))  # from memory, not from yesterday's lines

    def test_a_line_not_said_to_it_neither_opens_a_conversation_nor_keeps_one_open(self):
        kernel = self.kernel()
        self.say(kernel, "And now the weather.", addressed=False, speaker="unknown")
        self.assertIsNone(kernel.state.spoke_at)
        self.say(kernel, "Hello Sage.")
        self.clock.now += policy.SESSION_IDLE_S - 10
        self.say(kernel, "Rain is coming in from the west.", addressed=False, speaker="unknown")
        self.later(kernel, 20)  # 5 minutes after the line said to it, 20 s after the TV's
        self.assertEqual(len(self.rows(kernel, "session")), 1)

    def test_a_line_said_after_the_quiet_starts_a_new_conversation_even_if_no_tick_ran(self):
        kernel = self.kernel()
        self.say(kernel, "Hello Sage.")
        self.clock.now += policy.SESSION_IDLE_S + 1
        self.say(kernel, "Are you there?")
        self.assertEqual((len(self.rows(kernel, "session")), kernel.state.said, len(kernel.state.exchanges)), (1, 1, 1))

    def test_a_long_conversation_is_closed_at_forty_lines_and_goes_on_with_its_last_exchanges(self):
        kernel = self.kernel()
        for n in range(policy.SESSION_TURNS + 1):
            self.say(kernel, f"Thing number {n} about my garden.")
        self.assertEqual(self.rows(kernel, "session"), [("kernel", {"turns": policy.SESSION_TURNS, "reason": "cap"})])
        self.assertEqual(kernel.state.said, 1)
        self.assertTrue(1 <= len(kernel.state.exchanges) <= policy.KEPT + 1)
        self.assertEqual(self.wrote, [])  # mid-conversation nobody is kept waiting for a writer

    def test_a_typed_line_is_said_to_it(self):
        kernel = self.kernel()
        self.say(kernel, "My name is Wren.", addressed=False, speaker="unknown", input={"medium": "text"})
        self.assertEqual(kernel.state.said, 1)

    def test_the_idle_time_is_a_setting(self):
        kernel = self.kernel(idle_s=30)
        self.say(kernel, "Hello Sage.")
        self.later(kernel, 31)
        self.assertEqual(len(self.rows(kernel, "session")), 1)


class TheWriter(Home):
    def test_s1_what_the_owner_mentioned_is_a_record_after_the_conversation_and_in_the_next_one_s_prompt(self):
        kernel = self.kernel()
        self.writes = lambda prompt: {"operations": [{"op": "add", "kind": "thread", "text": "Their sister visits on Friday.",
                                                      "from": cited(prompt, "My sister visits on Friday.")}],
                                      "day": "They talked about a family visit."}
        self.say(kernel, "My sister visits on Friday.")
        self.assertEqual(kernel.state.facts, ())  # nothing mid-conversation
        self.later(kernel)
        thread, day = kernel.state.facts
        self.assertEqual((thread["who"], thread["kind"], thread["text"]), ("owner", "thread", "Their sister visits on Friday."))
        self.assertEqual(self.rows(kernel, "submitted")[0][1]["text"], "My sister visits on Friday.")
        self.assertEqual(kernel.journal.line(thread["from"]), "My sister visits on Friday.")
        self.assertEqual((day["kind"], day["text"]), ("day", "They talked about a family visit."))
        self.assertEqual([source for source, _ in self.rows(kernel, "remember")], ["writer", "writer"])
        self.say(kernel, "Good morning.")
        self.assertIn("Their sister visits on Friday.", self.asked[-1].system)

    def test_the_writer_sees_only_lines_said_to_sage_by_the_owner_or_a_voice_it_could_not_place(self):
        kernel = self.kernel()
        self.say(kernel, "I work at the bakery on Mill Lane.")
        self.say(kernel, "My name is Lena, from the show.", addressed=False, speaker="unknown")  # the TV (s6)
        self.say(kernel, "I am Tomas, their neighbour.", speaker="guest")  # a guest (s7)
        self.say(kernel, "I keep the spare key under the", input={"medium": "speech", "confidence": 0.4})  # misheard (s8)
        self.say(kernel, "The cat is called Biscuit.", speaker="unknown")
        self.assertIn("Tomas", json.dumps(self.asked[-1].context))  # a guest is in the conversation while it lasts
        self.later(kernel)
        shown = self.wrote[0].context[-1]["content"]
        self.assertIn("I work at the bakery on Mill Lane.", shown)
        self.assertIn("The cat is called Biscuit.", shown)
        for never in ("Lena", "Tomas", "spare key"):
            self.assertNotIn(never, shown)
        self.assertIn("Reply 1.", shown)  # what Sage answered, for a promise; never a line to cite
        self.say(kernel, "Hello again.")
        self.assertNotIn("Tomas", json.dumps(self.asked[-1].messages()))  # and gone afterwards

    def test_an_operation_that_cites_no_such_line_is_refused(self):
        kernel = self.kernel()
        self.say(kernel, "I am Tomas, their neighbour.", speaker="guest")
        self.say(kernel, "I like oolong tea.")
        guest = self.sequence(kernel, "I am Tomas, their neighbour.")

        def writes(prompt):
            mine = cited(prompt, "I like oolong tea.")
            return {"operations": [{"op": "add", "kind": "person", "text": "Their neighbour is Tomas.", "from": guest},
                                   {"op": "add", "kind": "person", "text": "Tomas is here.", "from": mine + 1000},
                                   {"op": "add", "kind": "like", "text": "Likes oolong tea.", "from": mine},
                                   {"op": "add", "kind": "like", "text": "No line at all."},
                                   {"op": "add", "kind": "secret", "text": "A kind there is not.", "from": mine},
                                   {"op": "drop", "id": 99, "from": mine}], "day": ""}
        self.writes = writes
        self.later(kernel)
        self.assertEqual(self.texts(kernel), ["Likes oolong tea."])
        (_, written), = self.rows(kernel, "written")
        self.assertEqual(len(written["refused"]), 5)  # the sixth is over the five it may make
        self.assertTrue(all(refusal["why"] for refusal in written["refused"]))

    def test_at_most_five_operations_are_kept(self):
        kernel = self.kernel()
        self.say(kernel, "I like oolong tea.")
        self.writes = lambda prompt: {"operations": [{"op": "add", "kind": "like", "text": f"Likes tea number {n}.",
                                                      "from": cited(prompt, "I like oolong tea.")} for n in range(8)], "day": ""}
        self.later(kernel)
        self.assertEqual(len(kernel.state.facts), policy.MAX_OPS)

    def test_s10_a_correction_updates_the_record_and_a_line_can_drop_one(self):
        kernel = self.kernel()
        self.say(kernel, "My sister visits on Friday.")
        self.writes = lambda prompt: {"operations": [{"op": "add", "kind": "thread", "text": "Their sister visits on Friday.",
                                                      "from": cited(prompt, "My sister visits on Friday.")}], "day": ""}
        self.later(kernel)
        self.say(kernel, "It's Saturday, not Friday.")
        self.writes = lambda prompt: {"operations": [{"op": "update", "id": 1, "text": "Their sister visits on Saturday.",
                                                      "from": cited(prompt, "It's Saturday, not Friday.")}], "day": ""}
        self.later(kernel)
        self.assertEqual(self.texts(kernel), ["Their sister visits on Saturday."])
        self.assertIn("[1] thread: Their sister visits on Friday.", self.wrote[1].context[-1]["content"])
        self.say(kernel, "The visit is off.")
        self.writes = lambda prompt: {"operations": [{"op": "drop", "id": 1, "from": cited(prompt, "The visit is off.")}], "day": ""}
        self.later(kernel)
        self.assertEqual(kernel.state.facts, ())
        self.assertEqual(self.rows(kernel, "forget")[0][0], "writer")

    def test_a_voice_it_could_not_place_is_remembered_as_unknown_and_never_changes_what_the_owner_said(self):
        kernel = self.kernel()
        self.say(kernel, "My name is Wren.")
        self.writes = lambda prompt: {"operations": [{"op": "add", "kind": "person", "text": "Their name is Wren.",
                                                      "from": cited(prompt, "My name is Wren.")}], "day": ""}
        self.later(kernel)

        def writes(prompt):
            line = cited(prompt, "My name is Ada, actually.")
            return {"operations": [{"op": "update", "id": 1, "text": "Their name is Ada.", "from": line},
                                   {"op": "add", "kind": "person", "text": "Someone here is called Ada.", "from": line}],
                    "day": ""}
        self.writes = writes
        self.say(kernel, "My name is Ada, actually.", speaker="unknown")  # no voiceprint enrolled: nobody is the owner
        self.later(kernel)
        self.assertEqual([(r["who"], r["text"]) for r in kernel.state.facts],
                         [("owner", "Their name is Wren."), ("unknown", "Someone here is called Ada.")])
        system = policy.compile_prompt(policy.focus(kernel.state, "reachy"), "reachy", BODY).system
        self.assertLess(system.index("Their name is Wren."), system.index(policy.UNPLACED))
        self.assertLess(system.index(policy.UNPLACED), system.index("Someone here is called Ada."))
        self.assertIn("never greet", policy.UNPLACED)

    def test_the_writer_s_prompt_stays_small_however_much_is_remembered_or_said(self):
        facts = tuple(record(n, f"Keeps bees in hive number {n} " + "by the old wall " * 5, kind="like") for n in range(1, 150))
        facts += (record(150, "Their tomatoes won a prize."),)
        lines = [(n, "owner", "How are my tomatoes doing? " * 40, "They look well. " * 40) for n in range(300, 340)]
        asked = policy.writer_prompt(facts, lines).context[-1]["content"]
        self.assertLess(len(policy.WRITER["writer"]) + len(asked), 16_000)  # under the model's 5,120 tokens
        self.assertIn("[150] person: Their tomatoes won a prize.", asked)  # the record the lines are about
        self.assertLessEqual(asked.count("Keeps bees"), policy.WRITER_KNOWN)

    def test_a_conversation_with_no_line_for_the_writer_asks_no_model_and_is_not_read_again(self):
        kernel = self.kernel()
        self.say(kernel, "I am Tomas, their neighbour.", speaker="guest")
        self.later(kernel)
        self.assertEqual((self.wrote, kernel.state.due), ([], 0))
        self.assertGreater(kernel.state.cursor, 0)

    def test_s14_a_turn_that_arrives_while_the_writer_runs_is_answered_and_the_writer_runs_again_later(self):
        kernel = self.kernel()
        entered, release, dropped, hung = threading.Event(), threading.Event(), [], []

        def slow(prompt, heard):
            self.wrote.append(prompt)
            entered.set()
            release.wait(5)
            try:
                heard("{")  # its next piece, after a turn arrived: the call ends here
            except Abandoned:
                dropped.append(True)
                raise
            return json.dumps({"operations": [{"op": "add", "kind": "like", "text": "Likes oolong tea.",
                                               "from": cited(prompt, "I like oolong tea.")}], "day": ""})
        slow.hang_up = lambda: hung.append(True)  # a real provider closes its socket here (WriterProvider)
        kernel.write = slow
        self.say(kernel, "I like oolong tea.")
        self.clock.now += policy.SESSION_IDLE_S + 1
        kernel.tick()
        self.assertTrue(entered.wait(5))
        began = time.monotonic()
        self.assertEqual(self.say(kernel, "Are you there?"), "Reply 2.")
        self.assertLess(time.monotonic() - began, 2)  # never behind the writer
        self.assertEqual(hung, [True])  # hung up the moment the line arrived
        release.set()
        self.assertTrue(until(lambda: not kernel.writing))
        self.assertEqual((dropped, kernel.state.facts), ([True], ()))
        release.clear()
        entered.clear()
        self.clock.now += policy.SESSION_IDLE_S + 1
        kernel.tick()
        self.assertTrue(entered.wait(5))
        release.set()  # nobody speaks this time
        self.assertTrue(until(lambda: self.texts(kernel) == ["Likes oolong tea."]))

    def test_a_writer_that_fails_is_asked_again_later_and_one_that_answers_nonsense_is_not(self):
        kernel = self.kernel()
        self.say(kernel, "I like oolong tea.")

        def down(prompt):
            raise OSError("the model server is down")
        self.writes = down
        self.later(kernel)
        self.assertEqual((len(self.wrote), kernel.state.due), (1, 1))
        self.later(kernel, 1)
        self.assertEqual(len(self.wrote), 1)  # not every tick
        self.writes = lambda prompt: "not what was asked for"
        self.later(kernel, 120)
        self.assertEqual((len(self.wrote), kernel.state.due, kernel.state.facts), (2, 0, ()))
        self.later(kernel, 120)
        self.assertEqual(len(self.wrote), 2)

    def test_without_a_writer_conversations_still_end_and_nothing_is_written(self):
        kernel = self.kernel(write=None)
        self.say(kernel, "I like oolong tea.")
        self.later(kernel)
        self.assertEqual((kernel.state.exchanges, kernel.state.facts, kernel.state.due), ((), (), 1))

    def test_day_lines_and_open_threads_grow_old_and_who_they_are_does_not(self):
        facts = (record(1, "Their name is Wren.", at=0.0), record(2, "Their sister visits on Friday.", kind="thread", at=0.0),
                 record(3, "They talked about soup.", kind="day", at=0.0))
        state, _ = policy.written(Companion(facts=facts, due=1), 9, [(5, "owner", "I like oolong tea.", "")],
                                  json.dumps({"operations": [{"op": "add", "kind": "like", "text": "Likes oolong tea.",
                                                              "from": 5}], "day": ""}), policy.STALE_S + 1)
        self.assertEqual([r["text"] for r in state.facts], ["Their name is Wren.", "Likes oolong tea."])
        self.assertEqual((state.cursor, state.due), (9, 0))


class Recall(unittest.TestCase):
    def test_the_block_never_passes_its_budget_however_much_is_remembered(self):
        facts = tuple(record(n, f"Fact number {n} " + "about the allotment " * 5, kind=("person", "like", "thread")[n % 3],
                             who=("owner", "unknown")[n % 2], at=float(n)) for n in range(1, 60))
        block = policy.recall(facts)
        self.assertLessEqual(len(block), policy.RECALL_CHARS)
        self.assertGreater(block.count("\n- "), 1)

    def test_who_they_are_comes_first_then_the_last_day_then_what_is_open(self):
        facts = (record(1, "They talked about soup.", kind="day", at=1.0), record(2, "They talked about the allotment.", kind="day", at=2.0),
                 record(3, "Their sister visits on Friday.", kind="thread", at=3.0), record(4, "Their name is Wren.", at=4.0))
        block = policy.recall(facts)
        self.assertNotIn("soup", block)  # only the last day line
        self.assertLess(block.index("Wren"), block.index("allotment"))
        self.assertLess(block.index("allotment"), block.index("Friday"))

    def test_with_nothing_remembered_the_block_says_so(self):
        self.assertTrue(policy.recall(()).endswith(policy.NOTHING_YET))

    def test_up_to_twenty_records_nothing_is_ranked_and_past_twenty_two_matching_ones_ride_on_the_newest_line(self):
        few = tuple(record(n, f"Keeps bees in hive {n}.", kind="like", at=float(n)) for n in range(1, 21))
        many = few + tuple(record(20 + n, text, kind="like", at=0.0) for n, text in enumerate(
            ("Grows tomatoes on the balcony.", "Their tomatoes won a prize.", "Tomatoes are their favourite.",
             "Plays the cello.")))

        def newest(facts):
            state = policy.submit(Companion(facts=facts), "reachy", "r1", "How are my tomatoes doing?", {}, False)[0]
            return policy.compile_prompt(state, "reachy", BODY).context[-1]["content"]
        self.assertEqual(newest(few[:19] + (record(20, "Their tomatoes won a prize.", kind="like", at=0.0),)).count("omatoes"), 1)
        self.assertEqual(newest(many).count("omatoes"), 1 + policy.RECALL_EXTRA)  # the line's own, and two records
        self.assertNotIn("cello", newest(many))

    def test_a_name_from_a_fact_never_leaves_in_a_web_query(self):
        self.assertEqual(policy.needs_search("Who is Mika?", SURE, ("Their name is Mika.",)), "")


class Spoken(Home):
    def remembered(self, *texts):
        kernel = self.kernel()
        with kernel.lock:
            state = kernel.state
            for text in texts:
                state, rows = policy.amend(state, "add", self.clock(), text=text)
                kernel._commit(state, rows)
        return kernel

    def test_s4_forget_about_something_drops_it_at_once_without_a_model_and_says_how_many(self):
        kernel = self.remembered("Takes their pills at nine.", "Keeps pills in the kitchen.", "Likes oolong tea.")
        said = self.say(kernel, "Forget about the pills.")
        self.assertEqual(self.texts(kernel), ["Likes oolong tea."])
        self.assertIn("2", said)
        self.assertEqual(self.asked, [])
        self.assertNotIn("pills", policy.recall(kernel.state.facts))  # gone from the very next prompt

    def test_forgetting_reads_the_words_named_and_an_idiom_is_not_a_command(self):
        kernel = self.remembered("Their name is Wren.", "Likes oolong tea.")
        for line in ("Don't forget to water the plants.", "I always forget where the keys are.", "What do you know?"):
            self.say(kernel, line)
        self.assertEqual((len(self.texts(kernel)), len(self.asked)), (2, 3))  # the model's, all three
        self.say(kernel, "Please forget my name.")
        self.assertEqual(self.texts(kernel), ["Likes oolong tea."])

    def test_what_was_said_before_a_forget_never_becomes_a_memory(self):
        kernel = self.kernel()
        self.say(kernel, "I take my pills at nine.")
        pills = self.sequence(kernel, "I take my pills at nine.")
        self.writes = lambda prompt: {"operations": [{"op": "add", "kind": "person", "text": "Takes their pills at nine.",
                                                      "from": pills}], "day": ""}
        self.assertTrue(self.say(kernel, "Forget that."))
        self.say(kernel, "I like oolong tea.")
        self.later(kernel)
        self.assertNotIn("pills", self.wrote[0].context[-1]["content"])  # the writer never reads it
        self.assertIn("oolong", self.wrote[0].context[-1]["content"])
        self.assertEqual(kernel.state.facts, ())  # and a writer that cites it anyway is refused
        self.assertEqual(len(self.rows(kernel, "written")[0][1]["refused"]), 1)

    def test_s5_asked_what_it_remembers_it_reads_back_what_fits_and_counts_the_rest(self):
        kernel = self.remembered(*[f"Keeps bees in hive {n}." for n in range(1, 9)])
        said = self.say(kernel, "What do you remember about me?")
        self.assertEqual(self.asked, [])
        self.assertEqual(said.count("Keeps bees"), policy.READ_BACK)
        self.assertTrue(said.endswith("And 3 more."), said)
        self.assertLessEqual(len(said), manifest.MAX_REPLY)

    def test_s13_with_nothing_remembered_it_says_so(self):
        self.assertEqual(self.say(self.kernel(), "What do you remember?"), "I don't remember anything about you yet.")

    def test_a_guest_or_a_line_not_said_to_it_cannot_make_it_forget_or_read_memory_out(self):
        kernel = self.remembered("Takes their pills at nine.")
        self.say(kernel, "Forget about the pills.", speaker="guest")
        self.say(kernel, "Forget about the pills.", addressed=False, speaker="unknown")
        self.say(kernel, "What do you remember about me?", speaker="guest")
        self.assertEqual((self.texts(kernel), len(self.asked)), (["Takes their pills at nine."], 3))

    def test_remember_this_is_answered_by_the_model_and_written_after_the_conversation(self):
        kernel = self.kernel()
        self.assertEqual(self.say(kernel, "Remember, I take my pills at nine."), "Reply 1.")  # s3: confirmed in words
        self.assertEqual(kernel.state.facts, ())


class OwnerCommand(Home):
    def run_memory(self, *args, directory=None):
        out = []
        code = memory(list(args), str(directory or self.dir), out.append)
        return code, "\n".join(out)

    def test_list_shows_each_record_with_the_line_it_came_from(self):
        kernel = self.kernel()
        self.writes = lambda prompt: {"operations": [{"op": "add", "kind": "like", "text": "Likes oolong tea.",
                                                      "from": cited(prompt, "I like oolong tea.")}], "day": ""}
        self.say(kernel, "I like oolong tea.")
        self.later(kernel)
        code, out = self.run_memory("list")
        self.assertEqual(code, 0)
        self.assertRegex(out, r'1\s+owner\s+like\s+\S+\s+Likes oolong tea\.\s+<- line \d+: "I like oolong tea\."')

    def test_add_edit_and_forget_reach_a_running_kernel_and_are_journaled_as_the_owner_s(self):
        kernel = self.kernel()
        self.say(kernel, "Hello Sage.")
        self.assertEqual(self.run_memory("add", "Their name is  Wren.")[0], 0)
        self.assertEqual(self.say(kernel, "Hello again."), "Reply 2.")  # the kernel's next row does not collide
        self.assertIn("- Their name is Wren.", self.asked[-1].system)
        self.assertEqual(self.run_memory("edit", "1", "Their name is Ada.")[0], 0)
        self.say(kernel, "And again.")
        self.assertIn("- Their name is Ada.", self.asked[-1].system)
        self.assertNotIn("Wren", self.asked[-1].system)
        self.assertEqual(self.run_memory("forget", "1")[0], 0)
        self.say(kernel, "Once more.")
        self.assertNotIn("Ada", self.asked[-1].system)  # a forgotten record never appears again
        self.assertEqual(Journal(kernel.journal.path).state.facts, ())  # nor after a restart
        self.assertEqual([source for source, _ in self.rows(kernel, "remember") + self.rows(kernel, "forget")], ["owner"] * 3)
        self.assertEqual(len(kernel.state.exchanges), 4)  # the owner's change touched nothing else

    def test_a_wrong_id_wrong_words_or_a_wrong_folder_change_nothing(self):
        self.kernel()
        self.assertEqual(self.run_memory("forget", "7")[0], 1)
        self.assertEqual(self.run_memory("edit", "one", "x")[0], 1)
        self.assertEqual(self.run_memory("frobnicate")[0], 2)
        self.assertEqual(self.run_memory()[0], 2)
        empty = self.dir / "elsewhere"
        empty.mkdir()
        code, out = self.run_memory("list", directory=empty)
        self.assertEqual((code, list(empty.iterdir())), (1, []))  # never makes a journal where there was none
        self.assertIn(str(empty), out)

    def test_the_command_runs_as_python_m_sage_memory_on_the_folder_the_environment_names(self):
        kernel = self.kernel()
        with kernel.lock:
            kernel._commit(*policy.amend(kernel.state, "add", 1.0, text="Likes oolong tea."))
        env = {key: value for key, value in os.environ.items() if not key.startswith("SAGE_")}
        done = subprocess.run([sys.executable, "-m", "sage", "memory", "list"], cwd=ROOT, capture_output=True, text=True,
                              env={**env, "SAGE_STATE_DIR": str(self.dir)}, timeout=30, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Likes oolong tea.", done.stdout)


class WriterProvider(unittest.TestCase):
    """sage.adapters.model.writer: the local model's call for memory, which a turn can end at once."""

    def serve(self, answer):
        test, self.requests, self.release = self, [], threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                test.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                if answer is None:
                    test.release.wait(10)  # a model still reading its prompt: no byte yet
                    return
                self.send_response(200)
                self.end_headers()
                for piece, done in ((answer[:5], False), (answer[5:], False), ("", True)):
                    self.wfile.write(json.dumps({"message": {"content": piece}, "done": done}).encode() + b"\n")

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(lambda: (self.release.set(), server.shutdown(), server.server_close()))
        return f"http://127.0.0.1:{server.server_port}"

    def test_it_asks_the_local_model_for_the_writer_s_json_with_the_talking_model_s_context_size(self):
        prompt = policy.writer_prompt((), [(7, "owner", "I like oolong tea.", "Noted.")])
        heard = []
        answer = model.writer(self.serve('{"operations": [], "day": ""}'), "qwen3:4b")(prompt, heard.append)
        self.assertEqual((answer, "".join(heard)), ('{"operations": [], "day": ""}',) * 2)
        sent, = self.requests
        self.assertEqual((sent["messages"], sent["format"], sent["think"]), (prompt.messages(), prompt.format, False))
        self.assertEqual(sent["options"]["num_ctx"], model.OPTIONS["num_ctx"])  # another size reloads the model
        self.assertEqual(sent["format"]["properties"]["operations"]["maxItems"], policy.MAX_OPS)

    def test_hanging_up_ends_a_call_still_waiting_for_its_first_piece(self):
        write, failed = model.writer(self.serve(None), "qwen3:4b"), []

        def call():
            try:
                write(policy.writer_prompt((), [(7, "owner", "I like oolong tea.", "")]), lambda piece: None)
            except OSError as error:
                failed.append(error)
        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        self.assertTrue(until(lambda: self.requests))
        began = time.monotonic()
        write.hang_up()
        thread.join(5)
        self.assertEqual((len(failed), thread.is_alive()), (1, False))
        self.assertLess(time.monotonic() - began, 2)

    def test_the_writer_is_never_a_remote_model_over_plain_http(self):
        with self.assertRaises(ValueError):
            model.writer("http://models.example", "qwen3:4b")


class OfflineScore(Home):
    """evals/memory.py: counts on a journal, and the writer on scripted conversations; never a heard line."""

    def test_sessions_counts_conversations_under_the_kernel_s_rule_and_the_writer_s_share(self):
        from evals import memory as scored
        kernel = self.kernel(write=None)
        for n in range(3):
            self.say(kernel, f"Line {n} of the first talk.")
            self.clock.now += 10
        self.say(kernel, "And now the weather.", addressed=False, speaker="unknown")
        self.clock.now += 400
        self.say(kernel, "I am Tomas, their neighbour.", speaker="guest")
        self.clock.now += 400
        self.say(kernel, "The evening news.", addressed=False, speaker="unknown")
        got = scored.sessions(scored.heard(kernel.journal.path))
        self.assertEqual((got["heard_lines"], got["said_to_sage"], got["writer_lines"], got["writer_calls"]), (6, 4, 3, 1))
        self.assertEqual(got["rule"], {"conversations": 2, "median": 3, "p90": 3, "longest": 3, "closed_at_cap": 0, "one_line": 1})
        self.assertEqual(got["pauses_over_all_lines"]["conversations"], 3)
        self.assertEqual(scored.conversations(range(100), 300, 40), [40, 40, 20])
        self.assertNotIn("Tomas", json.dumps(got))

    def test_the_writer_s_score_counts_what_was_kept_through_the_kernel_s_own_checks(self):
        from evals import memory as scored
        cases = json.loads(scored.SCRIPTED.read_text())["cases"]
        self.assertGreaterEqual(len(cases), 15)
        nothing = scored.score(lambda prompt, heard: json.dumps({"operations": [], "day": ""}), cases)
        # A writer that does nothing keeps nothing, and leaves the two records a line corrected or called off.
        self.assertEqual((nothing["kept"], nothing["wrong"], nothing["passes"]), (0, 2, False))

        def everything(prompt, heard):  # a writer that keeps every line it was shown, word for word
            shown = re.findall(r"^\[(\d+)\] (.+)$", prompt.context[-1]["content"].split(policy.WRITER["writer_said"])[1], re.MULTILINE)
            return json.dumps({"operations": [{"op": "add", "kind": "person", "text": text, "from": int(n)}
                                              for n, text in shown], "day": ""})
        greedy = scored.score(everything, cases)
        self.assertGreater(greedy["kept"], 0)
        self.assertGreater(greedy["wrong"], 0)  # it kept the small talk
        shown = []  # and even it was shown nothing of the TV's, a guest's or a half-heard line
        scored.score(lambda prompt, heard: shown.append(prompt.context[-1]["content"]) or "{}", cases)
        self.assertFalse(any(word in text for text in shown for word in ("Lena", "Tomas", "spare key")))


if __name__ == "__main__":
    unittest.main()
