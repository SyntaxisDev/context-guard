"""A temporary native package joins discovery and shared scenarios unchanged."""

import importlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import adapters
from adapters import registry
from tests import harness
from tests.conformance import discovery


class SyntheticAdapterTests(unittest.TestCase):
    """Exercise extension using temporary metadata, fixtures, decoder and hook process."""

    def test_new_package_is_discovered_and_participates_without_shared_edits(self) -> None:
        importlib.import_module("adapters.codex.tests.driver")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.seed_package(root)
            with (
                mock.patch.object(adapters, "__path__", [str(root / "adapters"), *adapters.__path__]),
                mock.patch.object(registry, "ROOT", root),
                mock.patch.object(harness, "ROOT", root),
                mock.patch.object(harness, "FIXTURES", root / "tests/fixtures"),
            ):
                importlib.invalidate_caches()
                try:
                    found = registry.discover()
                    self.assertEqual(list(found), ["synthetic"])
                    cases = list(discovery.suite())
                    self.assertTrue(cases)
                    self.assertTrue(all("synthetic." in case.id() for case in cases))
                    scenario = next(case for case in cases if case.id().endswith("test_prompt_warning_below_threshold"))
                    result = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite([scenario]))
                    self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
                    self.assertEqual(result.testsRun, 1)
                finally:
                    for name in list(sys.modules):
                        if name == "adapters.synthetic" or name.startswith("adapters.synthetic."):
                            del sys.modules[name]

    def seed_package(self, root: Path) -> None:
        """Create an isolated synthetic package using a recorded native protocol family."""
        package = root / "adapters/synthetic"
        (package / "tests").mkdir(parents=True)
        (package / "__init__.py").touch()
        (package / "tests/__init__.py").touch()
        (package / "adapter.py").write_text(
            "from core.features import Adapter\n"
            "ADAPTER = Adapter('synthetic', 'adapters.synthetic.hooks', 'adapters.synthetic.mapping', {})\n"
        )
        (package / "mapping.py").write_text("HOOK_EVENTS = ('UserPromptSubmit',)\n")
        (package / "hooks.py").write_text("import json,sys\njson.load(sys.stdin)\nprint('{}')\n")
        (package / "tests/driver.py").write_text(
            "from adapters.codex.tests.driver import Driver as NativeDriver\n"
            "class Driver(NativeDriver):\n    runtime = 'synthetic'\n"
        )
        (package / "tests/decoder.py").write_text('"""Uses the independently documented native response family."""\n')
        (root / "docs").mkdir()
        (root / "docs/synthetic.md").write_text("# Synthetic adapter\n")
        for name in ("synthetic", "codex"):
            shutil.copytree(harness.FIXTURES / "codex", root / "tests/fixtures" / name)
