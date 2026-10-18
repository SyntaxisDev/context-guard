"""AGENTS.md validation commands are runnable and generated doc tables are current."""
import re
import unittest
from pathlib import Path

from scripts import gen_docs

ROOT = Path(__file__).resolve().parents[1]
DISCOVER = re.compile(r"^uv run --python 3\.14 python -m unittest discover -s (\S+) -t \.$", re.M)
SCRIPT = re.compile(r"^uv run --python 3\.14 python (\S+\.py)\b", re.M)


class DocsTests(unittest.TestCase):
    def setUp(self):
        self.agents = (ROOT / "AGENTS.md").read_text()

    def test_discover_commands_target_test_directories(self):
        directories = DISCOVER.findall(self.agents)
        self.assertTrue(directories)
        for directory in directories:
            with self.subTest(directory=directory):
                tests = list((ROOT / directory).rglob("test_*.py"))
                self.assertTrue(tests, f"{directory} has no test_*.py")

    def test_every_adapter_suite_is_listed(self):
        listed = set(DISCOVER.findall(self.agents))
        for adapter in sorted((ROOT / "adapters").iterdir()):
            if (adapter / "tests").is_dir():
                self.assertIn(f"adapters/{adapter.name}/tests", listed)

    def test_script_commands_exist(self):
        for script in SCRIPT.findall(self.agents):
            with self.subTest(script=script):
                self.assertTrue((ROOT / script).is_file())

    def test_generated_tier_tables_are_current(self):
        for runtime in gen_docs.RUNTIMES:
            with self.subTest(runtime=runtime):
                self.assertFalse(gen_docs.stale(runtime), "run scripts/gen_docs.py")


if __name__ == "__main__":
    unittest.main()
