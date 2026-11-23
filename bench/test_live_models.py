"""The owner's live turns come first: a model run starts only after a quiet spell, and stops on a turn."""
import json
import os
import re
import time
import unittest

import live_models

LINE = "2026-11-08 13:22:13,007 499866.1 reachy-x INFO:reachy_sage_voice.sage:turn start (kernel lane): voice ended 600 ms ago"
OTHER = "2026-11-08 13:30:00,000 499900.0 - INFO:reachy_sage_voice.motion.runner:motion gates: 2997 ticks"


def at(clock):
    return live_models.stamp("2026-11-08 " + clock)


class Quiet(unittest.TestCase):
    def test_a_turn_within_the_spell_is_not_quiet(self):
        self.assertFalse(live_models.quiet([LINE], at("13:24:00"), 180))

    def test_a_turn_older_than_the_spell_is_quiet(self):
        self.assertTrue(live_models.quiet([LINE], at("13:25:14"), 180))

    def test_lines_that_are_not_turns_do_not_count(self):
        self.assertTrue(live_models.quiet([OTHER], at("13:30:01"), 180))

    def test_a_turn_after_a_run_began_is_seen(self):
        self.assertTrue(live_models.turned_since([LINE], at("13:22:00")))
        self.assertFalse(live_models.turned_since([LINE], at("13:22:14")))


class Stop(unittest.TestCase):
    def test_stopping_a_run_ends_what_it_started_too(self):
        """The eval starts its own kernel: stopping the run must not leave it asking for the model."""
        import os
        import subprocess
        import sys
        child = subprocess.Popen([sys.executable, "-c", "import subprocess,sys,time;"
                                  "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                                  "print(p.pid,flush=True);time.sleep(60)"],
                                 stdout=subprocess.PIPE, text=True, start_new_session=True)
        grandchild = int(child.stdout.readline())
        live_models.stop(child)
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(grandchild, 0)


class Brain(unittest.TestCase):
    """The brain comparison: every candidate answers the same made-up lines, scored the same way. No model here."""
    KERNEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

    def scenarios(self):
        with open(live_models.SCENARIOS) as f:
            return json.load(f)

    def test_the_scenarios_cover_the_four_kinds_and_their_patterns_compile(self):
        scenarios = self.scenarios()
        kinds = [s["kind"] for s in scenarios]
        self.assertEqual({k: kinds.count(k) for k in set(kinds)},
                         {"question": 6, "half_heard": 6, "unknowable": 6, "follow_up": 6})
        self.assertEqual(len({s["id"] for s in scenarios}), len(scenarios))
        for s in scenarios:
            for pattern in s["expect"]:
                re.compile(pattern)

    def test_a_reply_is_scored_on_what_it_says(self):
        capital = {"kind": "question", "expect": ["tokyo"]}
        pocket = {"kind": "unknowable", "expect": ["can(no|')t (know|see)"]}
        score = live_models.score
        self.assertEqual(score(capital, '{"reply": "Tokyo!"}'), {"schema": True, "dont_know": False, "correct": True})
        self.assertEqual(score(capital, '{"reply": "Hmm, I don\'t know."}'),
                         {"schema": True, "dont_know": True, "correct": False})
        self.assertEqual(score(capital, '{"reply": "Kyoto."}'), {"schema": True, "dont_know": False, "correct": False})
        self.assertTrue(score(pocket, '{"reply": "I can\'t see your pocket."}')["correct"])
        self.assertTrue(score(pocket, '{"reply": "I don\'t know, tell me?"}')["correct"])
        self.assertFalse(score(pocket, '{"reply": "Your keys."}')["correct"])
        for broken in ('{"reply": "Tok', '["Tokyo"]', '{"reply": 3}'):
            self.assertEqual(score(capital, broken), {"schema": False, "dont_know": False, "correct": False})

    def test_every_scenario_reaches_the_candidate_as_the_kernel_s_own_prompt_and_is_timed(self):
        prompts = []

        def decide(prompt, heard):
            prompts.append(prompt)
            heard('{"reply": "Tokyo')
            heard('."}')
            return '{"reply": "Tokyo."}'
        scenarios = self.scenarios()
        results = live_models.answer_all(self.KERNEL, decide, scenarios, runs=1)
        self.assertEqual(len(results), len(scenarios))
        by_id = dict(zip((s["id"] for s in scenarios), prompts))
        self.assertIn("capital of Japan", by_id["capital"].context[-1]["content"])
        self.assertIn("(unclear)", by_id["fronds"].context[-1]["content"])
        self.assertIn("Biscuit", by_id["cat"].system)
        self.assertEqual(["Italy" in by_id["spain"].context[0]["content"], "Rome" in by_id["spain"].context[1]["content"]],
                         [True, True])
        first = results[0]
        self.assertEqual((first["id"], first["correct"], first["reply"]), ("capital", True, "Tokyo."))
        self.assertLessEqual(first["first_s"], first["full_s"])

    def test_the_summary_reports_correct_dont_know_and_the_two_times(self):
        results = [{"kind": "question", "schema": True, "correct": True, "dont_know": False, "first_s": 0.2, "full_s": 0.5},
                   {"kind": "question", "schema": True, "correct": False, "dont_know": True, "first_s": 0.4, "full_s": 0.9},
                   {"kind": "unknowable", "schema": True, "correct": True, "dont_know": True, "first_s": 0.6, "full_s": 1.0},
                   {"kind": "follow_up", "schema": False, "correct": False, "dont_know": False, "first_s": 0.8, "full_s": 2.0,
                    "timing": {"prompt_tokens": 4000, "gen_tokens": 100}}]
        row = live_models.summary("anthropic:claude-haiku-5-5", results)
        self.assertEqual((row["answered_correctly"], row["dont_know_rate"], row["schema_failures"]), (50.0, 33.3, 1))
        self.assertEqual((row["first_p50"], row["first_p95"], row["full_p50"], row["full_p95"]), (0.5, 0.8, 0.95, 2.0))
        self.assertEqual(row["by_kind"], {"question": 50.0, "unknowable": 100.0, "follow_up": 0.0})
        self.assertAlmostEqual(row["cost_per_turn_usd"], (4000 * 0.10 + 100 * 0.50) / 1e6)

    def test_hosted_is_adopted_only_when_it_clears_the_margin_and_the_speed_bar(self):
        def row(candidate, correct, first_p50=0.5, first_p95=1.5, schema_failures=0):
            return {"candidate": candidate, "answered_correctly": correct, "first_p50": first_p50,
                    "first_p95": first_p95, "schema_failures": schema_failures}
        local = [row("qwen3:4b-instruct", 40.0), row("qwen2.5:7b", 45.0)]
        margin = live_models.ADOPT_MARGIN
        verdict = live_models.verdict
        self.assertEqual(verdict([*local, row("anthropic:a", 45.0 + margin)]), {"anthropic:a": "adopt"})
        self.assertIn("margin", verdict([*local, row("anthropic:a", 44.9 + margin)])["anthropic:a"])
        self.assertIn("first piece", verdict([*local, row("anthropic:a", 90.0, first_p95=9.0)])["anthropic:a"])
        self.assertIn("schema", verdict([*local, row("anthropic:a", 90.0, schema_failures=1)])["anthropic:a"])
        self.assertIn("no local", verdict([row("anthropic:a", 90.0)])["anthropic:a"])
        # A later row for the same candidate replaces its earlier one.
        self.assertEqual(verdict([*local, row("anthropic:a", 10.0), row("anthropic:a", 90.0)]), {"anthropic:a": "adopt"})



if __name__ == "__main__":
    unittest.main()
