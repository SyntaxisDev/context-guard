"""Explicit conformance suite, separate from structural unit-test discovery."""

import unittest


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    """Leave behavior execution to scripts/conformance.py.

    Args:
        loader: Discovery loader.
        tests: Already loaded package tests.
        pattern: Requested discovery pattern.

    Returns:
        An empty suite; structural checks run from tests/test_conformance_structure.py.
    """
    del loader, tests, pattern
    return unittest.TestSuite()
