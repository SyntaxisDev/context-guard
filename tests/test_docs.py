"""AGENTS.md lists every adapter suite and generated doc tables are current."""

import re
import unittest
from pathlib import Path

from scripts import gen_docs

ROOT = Path(__file__).resolve().parents[1]
DISCOVER = re.compile(r"^uv run --python 3\.14 python -m unittest discover -s (\S+) -t \.$", re.MULTILINE)


class DocsTests(unittest.TestCase):
    """AGENTS.md lists every adapter suite and the generated tier tables are current."""

    def test_every_adapter_suite_is_listed(self) -> None:
        listed = set(DISCOVER.findall((ROOT / "AGENTS.md").read_text()))
        for adapter in sorted((ROOT / "adapters").iterdir()):
            if (adapter / "tests").is_dir():
                self.assertIn(f"adapters/{adapter.name}/tests", listed)

    def test_generated_tier_tables_are_current(self) -> None:
        for runtime in gen_docs.RUNTIMES:
            with self.subTest(runtime=runtime):
                self.assertFalse(gen_docs.stale(runtime), "run scripts/gen_docs.py")


if __name__ == "__main__":
    unittest.main()
