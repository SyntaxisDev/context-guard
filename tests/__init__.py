"""Shared tests; discovering this package also runs every adapter's own suite."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_tests(
    loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load the shared tests, then each `adapters/<runtime>/tests` suite.

    Args:
        loader: The loader running discovery.
        standard_tests: The tests defined in this module.
        pattern: The discovery file pattern, or None for the default.

    Returns:
        The shared tests followed by every adapter suite.
    """
    standard_tests.addTests(loader.discover(str(ROOT / "tests"), pattern or "test*.py", str(ROOT)))
    for suite in sorted((ROOT / "adapters").glob("*/tests")):
        standard_tests.addTests(loader.discover(str(suite), pattern or "test*.py", str(ROOT)))
    return standard_tests
