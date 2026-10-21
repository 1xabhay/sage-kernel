"""Disposition: when Sage may speak first. Pure, with time passed in."""

import random
import unittest
from itertools import pairwise

from sage import disposition as d
from sage.disposition import Disposition

MIN = 60.0


def run(events, start=None, t=d.DEFAULT):
    state, said = start or Disposition(), []
    for kind, at, *text in events:
        state, out = d.step(state, kind, at, *text, t=t)
        said += [(name, at) for name in out]
    return state, said


class Greeting(unittest.TestCase):
    def test_a_first_arrival_is_greeted_once(self):
        _, said = run([("seen", 0), ("seen", 5), ("tick", 10)])
        self.assertEqual(said, [("greet", 0)])

    def test_a_return_is_greeted_only_after_a_long_enough_absence(self):
        _, said = run([("seen", 0), ("lost", 10), ("seen", 10 + d.GREET_AFTER_S - 1)])
        self.assertEqual(said, [("greet", 0)])
        _, said = run([("seen", 0), ("lost", 10), ("seen", 10 + d.GREET_AFTER_S)])
        self.assertEqual(said, [("greet", 0), ("greet", 10 + d.GREET_AFTER_S)])


class Quiet(unittest.TestCase):
    def test_asking_for_quiet_is_recognised_and_ordinary_lines_are_not(self):
        for line in ("Be quiet.", "Shh!", "Not now, I'm busy.", "Please stop talking.", "Leave me alone."):
            with self.subTest(line=line):
                self.assertTrue(d.asks_for_quiet(line))
        for line in ("It's quiet in here.", "Tell me a joke.", "Now what?"):
            with self.subTest(line=line):
                self.assertFalse(d.asks_for_quiet(line))

    def test_quiet_ends_when_the_person_speaks_to_it_or_after_its_time(self):
        state, _ = run([("seen", 0), ("spoke", 1, "Be quiet.")])
        self.assertEqual(d.state(state, 2), "quiet")
        self.assertEqual(d.state(state, 1 + d.QUIET_S + 1), "available")
        state, _ = run([("spoke", 5, "Hello again.")], state)
        self.assertEqual(d.state(state, 6), "engaged")

    def test_nothing_proactive_happens_in_quiet(self):
        events = [("seen", 0), ("spoke", 1, "Shh.")] + [("tick", t * MIN) for t in range(1, 29)]
        _, said = run(events)
        self.assertEqual(said, [("greet", 0)])
        _, said = run([("seen", 0), ("spoke", 1, "Shh."), ("lost", 2), ("seen", 2 + d.GREET_AFTER_S)])
        self.assertEqual(said, [("greet", 0)])  # no greeting back into quiet either


class Nudging(unittest.TestCase):
    def test_a_long_idle_presence_becomes_restless_and_is_nudged_within_budget(self):
        events = [("seen", 0)] + [("tick", t * MIN) for t in range(1, 121)]
        _, said = run(events)
        nudges = [at for name, at in said if name == "nudge"]
        self.assertTrue(nudges)
        self.assertGreaterEqual(nudges[0], d.RESTLESS_S)
        self.assertTrue(all(b - a >= d.NUDGE_COOLDOWN_S for a, b in pairwise(nudges)))

    def test_never_right_after_the_person_spoke(self):
        events = [("seen", 0)] + [e for t in range(1, 200) for e in (("spoke", t * 30.0, "Hm."), ("tick", t * 30 + 1))]
        _, said = run(events)
        self.assertEqual([name for name, _ in said], ["greet"])

    def test_states(self):
        state, _ = run([("seen", 0)])
        self.assertEqual(d.state(state, 1), "available")
        self.assertEqual(d.state(state, d.RESTLESS_S), "restless")
        self.assertEqual(d.state(Disposition(), 1), "available")  # nobody there: nothing to be restless about


class Timings(unittest.TestCase):
    def test_the_durations_are_configurable(self):
        fast = d.Timings(greet_after_s=1, nudge_after_idle_s=2, quiet_s=3)
        state, out = d.step(Disposition(), "lost", 0, t=fast)
        state, out = d.step(state, "seen", 1, t=fast)
        self.assertEqual(out, ("greet",))
        self.assertEqual(d.state(state, 3, t=fast), "restless")
        state, _ = d.step(state, "spoke", 4, "Shh.", t=fast)
        self.assertEqual((d.state(state, 6.9, t=fast), d.state(state, 7.1, t=fast)), ("quiet", "engaged"))

    def test_the_engaged_window_is_configurable(self):
        state, _ = d.step(Disposition(), "spoke", 0, "Hi", t=d.Timings(engaged_s=5))
        self.assertEqual((d.state(state, 4.9, t=d.Timings(engaged_s=5)), d.state(state, 5.1, t=d.Timings(engaged_s=5))),
                         ("engaged", "available"))

    def test_the_nudge_budget_gap_and_a_greeting_on_start_are_configurable(self):
        events = [("seen", 0)] + [("tick", float(t)) for t in range(1, 61)]
        tight = d.Timings(nudge_after_idle_s=2, nudge_gap_s=2, nudges_per_hour=5)
        _, said = run(events, t=tight)
        self.assertEqual(len([n for n, _ in said if n == "nudge"]), 5)
        _, out = d.step(Disposition(), "seen", 0, t=d.Timings(greet_on_start=False))
        self.assertEqual(out, ())


class Properties(unittest.TestCase):
    """Over generated event sequences, the promises hold whatever happens."""

    def sequences(self, count=300):
        rng = random.Random(20261020)
        lines = ("Hello.", "Be quiet.", "Tell me more.", "Shh.", "What time is it?", "Not now.")
        for _ in range(count):
            at, events = 0.0, []
            for _ in range(rng.randint(5, 120)):
                at += rng.choice((1, 10, MIN, 5 * MIN, 20 * MIN))
                kind = rng.choice(("seen", "lost", "spoke", "tick", "tick", "tick"))
                events.append((kind, at, rng.choice(lines)) if kind == "spoke" else (kind, at))
            yield events

    def test_never_more_nudges_in_any_hour_than_the_budget(self):
        for events in self.sequences():
            nudges = [at for name, at in run(events)[1] if name == "nudge"]
            for index, at in enumerate(nudges):
                self.assertLessEqual(sum(at <= other < at + 3600 for other in nudges[index:]), d.NUDGE_BUDGET)

    def test_never_a_proactive_turn_in_quiet(self):
        for events in self.sequences():
            state = Disposition()
            for kind, at, *text in events:
                quiet = d.state(state, at) == "quiet" and not (kind == "spoke" and not d.asks_for_quiet(*text))
                state, out = d.step(state, kind, at, *text)
                if quiet:
                    self.assertEqual(out, ())

    def test_at_most_one_greet_per_arrival(self):
        for events in self.sequences():
            state, greets = Disposition(), 0
            for kind, at, *text in events:
                if kind == "seen" and not state.present:
                    greets = 0
                state, out = d.step(state, kind, at, *text)
                greets += out.count("greet")
                self.assertLessEqual(greets, 1)


if __name__ == "__main__":
    unittest.main()
