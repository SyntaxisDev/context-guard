"""Keep production functions focused and core independent of adapters and runtime homes."""
import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
MAX_FUNCTION_LINES = 80
RUNTIME_HOMES = re.compile(r"\.(cursor|codex|claude)\b")


def production_paths():
    for directory in (ROOT / "core", ROOT / "adapters"):
        for path in directory.rglob("*.py"):
            if "tests" not in path.relative_to(directory).parts:
                yield path
    if (ROOT / "cli.py").exists():
        yield ROOT / "cli.py"


def imported_modules(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


class ArchitectureTests(unittest.TestCase):
    def test_function_size(self):
        for path in production_paths():
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    with self.subTest(path=str(path.relative_to(ROOT)), function=node.name):
                        self.assertLessEqual(
                            node.end_lineno - node.lineno + 1, MAX_FUNCTION_LINES,
                            "Split responsibilities into focused helpers rather than expanding the limit.",
                        )

    def test_core_does_not_import_adapters(self):
        for path in (ROOT / "core").rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for module in imported_modules(tree):
                with self.subTest(path=str(path.relative_to(ROOT)), module=module):
                    self.assertNotEqual(module.split(".")[0], "adapters")

    def test_core_does_not_mention_runtime_homes(self):
        for path in (ROOT / "core").rglob("*.py"):
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertIsNone(RUNTIME_HOMES.search(path.read_text()))


if __name__ == "__main__":
    unittest.main()
