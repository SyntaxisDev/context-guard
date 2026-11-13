"""Keep native outcome decoding independent of production hooks and mappings."""

import ast
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.registry import discover
from tests import harness
from tests.driver_support import load_driver


def import_targets(path: Path) -> list[str]:
    """Resolve static local imports, including imported names and relative helpers.

    Returns:
        Absolute import candidates referenced by the module.
    """
    package = ".".join(path.relative_to(harness.ROOT).with_suffix("").parts[:-1])
    found = []
    tree = ast.parse(path.read_text())
    dynamic_loaders = {"__import__"}
    importlib_names = {"importlib"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
            importlib_names.update(alias.asname or alias.name for alias in node.names if alias.name == "importlib")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = importlib.util.resolve_name("." * node.level + module, package)
            found.append(module)
            found.extend(f"{module}.{alias.name}" for alias in node.names)
            if module == "importlib":
                dynamic_loaders.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "import_module"
                )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        dynamic = isinstance(function, ast.Name) and function.id in dynamic_loaders
        dynamic = dynamic or (
            isinstance(function, ast.Attribute)
            and function.attr == "import_module"
            and isinstance(function.value, ast.Name)
            and function.value.id in importlib_names
        )
        if dynamic and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            found.append(node.args[0].value)
    return found


def decoder_import_problems(path: Path) -> list[str]:
    """Trace local dependencies and reject production hook/mapping decoding or aliases.

    Returns:
        Dependency chains that violate the independent-decoder boundary.
    """
    pending = [(path, [str(path.relative_to(harness.ROOT))])]
    seen = set()
    problems = []
    while pending:
        current, chain = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for name in import_targets(current):
            pieces = name.split(".")
            if pieces[0] == "adapters" and any(part in {"hooks", "mapping"} for part in pieces):
                problems.append(" -> ".join([*chain, name]))
                continue
            candidates = [harness.ROOT.joinpath(*pieces).with_suffix(".py")]
            candidates.extend(
                harness.ROOT.joinpath(*pieces[:index], "__init__.py") for index in range(1, len(pieces) + 1)
            )
            pending.extend((target, [*chain, name]) for target in candidates if target.is_file())
    return problems


class DriverBoundaryTests(unittest.TestCase):
    """All registered native drivers validate real output without renderer imports."""

    def test_dynamic_import_aliases_cannot_hide_production_decoders(self) -> None:
        sources = (
            "import importlib as lib\nlib.import_module('adapters.codex.hooks')\n",
            "from importlib import import_module as load\nload('adapters.codex.mapping')\n",
            "__import__('adapters.codex.hooks', fromlist=['respond'])\n",
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(harness, "ROOT", Path(directory)):
            path = Path(directory) / "decoder.py"
            for source in sources:
                with self.subTest(source=source):
                    path.write_text(source)
                    self.assertTrue(decoder_import_problems(path))

    def test_package_initializer_cannot_hide_production_decoder_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(harness, "ROOT", Path(directory)):
            root = Path(directory)
            (root / "helpers").mkdir()
            (root / "helpers/__init__.py").write_text("from adapters.codex.hooks import respond\n")
            (root / "helpers/decode.py").write_text("def decode(raw): return raw\n")
            path = root / "decoder.py"
            path.write_text("from helpers.decode import decode\n")
            self.assertTrue(decoder_import_problems(path))

    def test_decoder_imports_remain_independent_transitively(self) -> None:
        for runtime in discover():
            with self.subTest(runtime=runtime):
                path = harness.ROOT / "adapters" / runtime / "tests/driver.py"
                self.assertEqual(decoder_import_problems(path), [])

    def test_driver_runs_real_subprocess_and_decodes_native_block(self) -> None:
        for runtime in discover():
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                project = home / "project"
                (project / ".git").mkdir(parents=True)
                driver = load_driver(runtime, home, project, "boundary-session")
                driver.tokens(driver.window)
                outcome = driver.fire("prompt", prompt="Implement the feature")
                self.assertTrue(outcome.blocked)
                self.assertIn("Context check", outcome.user_text or outcome.text)
                self.assertTrue(outcome.raw)
                self.assertNotEqual(outcome.channel, "none")

    def test_missing_text_cannot_decode_as_delivery(self) -> None:
        for runtime in discover():
            with self.subTest(runtime=runtime):
                driver = load_driver(runtime, Path("/unused"), Path("/unused/project"), "decode")
                outcome = driver.decode({})
                self.assertFalse(outcome.text)
                self.assertFalse(outcome.blocked)
                self.assertEqual(outcome.channel, "none")

    def test_user_visible_system_message_is_not_model_context_delivery(self) -> None:
        for runtime in ("codex", "genie", "claude"):
            with self.subTest(runtime=runtime):
                driver = load_driver(runtime, Path("/unused"), Path("/unused/project"), "decode")
                outcome = driver.decode({"systemMessage": "Context check: user-visible only"})
                self.assertEqual(outcome.text, "")
                self.assertEqual(outcome.channel, "none")

    def test_checker_rejects_aliased_renderer_through_relative_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "adapters/example/tests"
            package.mkdir(parents=True)
            driver = package / "driver.py"
            driver.write_text("from .helper import decode\n")
            (package / "helper.py").write_text("from adapters.example import hooks as renderer\n")
            with patch.object(harness, "ROOT", root):
                problems = decoder_import_problems(driver)
            self.assertEqual(len(problems), 1)
            self.assertIn("helper", problems[0])
            self.assertIn("adapters.example.hooks", problems[0])

    def test_checker_follows_package_initializers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "adapters/example/tests"
            package.mkdir(parents=True)
            driver = package / "driver.py"
            driver.write_text("from . import helpers\n")
            helpers = package / "helpers"
            helpers.mkdir()
            (helpers / "__init__.py").write_text("import adapters.example.mapping as renderer\n")
            with patch.object(harness, "ROOT", root):
                problems = decoder_import_problems(driver)
            self.assertEqual(len(problems), 1)
            self.assertIn("adapters.example.mapping", problems[0])
