"""The docs stay honest: make/docs.py on a fixture, then on this checkout."""

import importlib.util
import shutil
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "make" / "docs.py"


@unittest.skipUnless(SCRIPT.exists() and (ROOT / ".git").exists() and shutil.which("git") and shutil.which("make"),
                     "checks the source tree; the image ships without it")
class DocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("docs", SCRIPT)
        cls.docs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.docs)

    def test_the_checker_finds_dead_links_and_unknown_targets_in_a_fixture(self):
        self.docs.selftest()

    def test_every_link_and_make_command_in_this_checkout_resolves(self):
        self.assertEqual(self.docs.check(), [])


if __name__ == "__main__":
    unittest.main()
