"""Compose shared contracts; discovery.py binds them to validated adapters."""

import unittest

from tests.conformance.base import ScenarioCase
from tests.conformance.test_boundaries import BoundaryContracts
from tests.conformance.test_context import ContextContracts
from tests.conformance.test_handoffs import HandoffContracts
from tests.conformance.test_installation import InstallationContracts
from tests.conformance.test_subagents import SubagentContracts


class ConformanceCase(
    ContextContracts, HandoffContracts, SubagentContracts, InstallationContracts, BoundaryContracts, ScenarioCase
):
    """The executable scenario surface registered once in catalogue.SCENARIOS."""


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    """Keep direct module loading adapter-free, like package discovery.

    Args:
        loader: Discovery loader.
        tests: Already collected unbound contract cases.
        pattern: Requested discovery pattern.

    Returns:
        An empty suite; use discovery.suite for validated adapter-bound cases.
    """
    del loader, tests, pattern
    return unittest.TestSuite()
