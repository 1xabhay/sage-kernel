"""The core imports only the standard library and itself; frameworks live in sage/adapters."""

import ast
import sys
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "sage"
CORE = {path.stem for path in PACKAGE.glob("*.py")} - {"__main__"}


class BoundaryTests(unittest.TestCase):
    def test_core_imports_only_the_standard_library_and_core(self):
        self.assertTrue({"policy", "manifest", "journal", "kernel"} <= CORE)
        for module in CORE:
            for node in ast.walk(ast.parse((PACKAGE / f"{module}.py").read_text())):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [f"{node.module}.{alias.name}" if node.module == "sage" else node.module
                               for alias in node.names] if isinstance(node, ast.ImportFrom) else [])
                for name in names:
                    with self.subTest(module=module, imports=name):
                        self.assertFalse(isinstance(node, ast.ImportFrom) and node.level, "use absolute imports")
                        top, _, rest = name.partition(".")
                        if top == "sage":
                            self.assertIn(rest.partition(".")[0], CORE, "the core may not import an adapter")
                        else:
                            self.assertIn(top, sys.stdlib_module_names)


if __name__ == "__main__":
    unittest.main()
