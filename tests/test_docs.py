"""Generated doc tables are current."""

import unittest

from scripts import gen_docs


class DocsTests(unittest.TestCase):
    """The generated tier tables are current."""

    def test_generated_tier_tables_are_current(self) -> None:
        for runtime in gen_docs.RUNTIMES:
            with self.subTest(runtime=runtime):
                self.assertFalse(gen_docs.stale(runtime), "run scripts/gen_docs.py")


if __name__ == "__main__":
    unittest.main()
