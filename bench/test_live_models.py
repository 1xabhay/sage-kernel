"""The owner's live turns come first: a model run starts only after a quiet spell, and stops on a turn."""
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


import time  # noqa: E402

if __name__ == "__main__":
    unittest.main()
