"""Keep production functions focused and core independent of adapters and runtime homes."""

import ast
import re
import unittest
from collections.abc import Iterator
from pathlib import Path

from adapters.registry import discover

ROOT = Path(__file__).resolve().parents[1]
MAX_FUNCTION_LINES = 80
RUNTIME_HOMES = re.compile(r"\.(?:" + "|".join(re.escape(runtime) for runtime in discover()) + r")\b")


def production_paths() -> Iterator[Path]:
    for directory in (ROOT / "core", ROOT / "adapters"):
        for path in directory.rglob("*.py"):
            if "tests" not in path.relative_to(directory).parts:
                yield path
    if (ROOT / "cli.py").exists():
        yield ROOT / "cli.py"


def code_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    body = node.body
    if len(body) > 1 and ast.get_docstring(node) is not None:
        body = body[1:]
    return node.end_lineno - body[0].lineno + 1


def imported_modules(tree: ast.Module) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


class ArchitectureTests(unittest.TestCase):
    """Production functions stay small and core stays independent of adapters and runtime homes."""

    def test_registered_runtime_homes_are_forbidden(self) -> None:
        for runtime in discover():
            with self.subTest(runtime=runtime):
                self.assertIsNotNone(RUNTIME_HOMES.search(f"~/.{runtime}/settings.json"))

    def test_function_size(self) -> None:
        for path in production_paths():
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    with self.subTest(path=str(path.relative_to(ROOT)), function=node.name):
                        self.assertLessEqual(
                            code_lines(node),
                            MAX_FUNCTION_LINES,
                            "Split responsibilities into focused helpers rather than expanding the limit.",
                        )

    def test_core_does_not_import_adapters(self) -> None:
        for path in (ROOT / "core").rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for module in imported_modules(tree):
                with self.subTest(path=str(path.relative_to(ROOT)), module=module):
                    self.assertNotEqual(module.split(".")[0], "adapters")

    def test_core_does_not_mention_runtime_homes(self) -> None:
        for path in (ROOT / "core").rglob("*.py"):
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertIsNone(RUNTIME_HOMES.search(path.read_text()))


if __name__ == "__main__":
    unittest.main()
