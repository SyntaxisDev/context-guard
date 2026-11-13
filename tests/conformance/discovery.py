"""Validate complete infrastructure before constructing shared conformance cases."""

import ast
import importlib
import unittest
from collections.abc import Mapping
from pathlib import Path

from adapters.registry import discover, validate_declaration, validate_documentation
from core.features import Adapter, Feature, GapKind, Guarantee
from tests import harness
from tests.conformance.catalogue import DRIVER_METHODS, SCENARIOS
from tests.conformance.test_cases import ConformanceCase
from tests.driver_support import Operations
from tests.test_driver_boundary import decoder_import_problems


def validate_scenarios(scenarios: Mapping = SCENARIOS) -> None:
    """Reject missing contracts, unknown contracts and non-executable scenarios.

    Args:
        scenarios: Contract to executable scenario names.

    Raises:
        ValueError: Inventory is incomplete or invalid.
    """
    expected = set(Feature) | set(Guarantee)
    if set(scenarios) != expected:
        msg = f"Scenario contracts differ: missing={expected - set(scenarios)}, unknown={set(scenarios) - expected}"
        raise ValueError(msg)
    names = []
    for contract, registered in scenarios.items():
        if not registered:
            msg = f"Missing scenarios: {contract.value}"
            raise ValueError(msg)
        for name in registered:
            if not callable(getattr(ConformanceCase, f"test_{name}", None)):
                msg = f"Missing executable scenario: {contract.value}/{name}"
                raise ValueError(msg)  # noqa: TRY004
            names.append(name)
    if len(names) != len(set(names)):
        msg = "Duplicate shared scenario registration"
        raise ValueError(msg)
    methods = {name.removeprefix("test_") for name in dir(ConformanceCase) if name.startswith("test_")}
    if methods != set(names):
        msg = f"Unregistered executable scenarios: {methods - set(names)}"
        raise ValueError(msg)


def validate_decoder(path: Path) -> None:
    """Reject production hook or mapping dependencies in independent decoders.

    Args:
        path: Decoder source path.

    Raises:
        ValueError: Decoder imports production hook or mapping code.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
            modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
        if any(
            module.startswith("adapters.") and any(part in {"hooks", "mapping"} for part in module.split("."))
            for module in modules
        ):
            msg = f"Decoder imports production hooks/mapping: {path}"
            raise ValueError(msg)


def validate_adapter(adapter: Adapter) -> None:
    """Require native drivers, fixtures, documentation and complete event coverage.

    Args:
        adapter: Validated production declaration.

    Raises:
        ValueError: Required infrastructure is absent.
    """
    for gap in adapter.gaps.values():
        validate_documentation(adapter.name, gap.documentation)
    module = importlib.import_module(f"adapters.{adapter.name}.tests.driver")
    for method in DRIVER_METHODS:
        if not callable(getattr(module.Driver, method, None)):
            msg = f"Missing driver operation: {adapter.name}/{method}"
            raise ValueError(msg)  # noqa: TRY004
    for method in ("payload", "tokens", "decode"):
        if getattr(module.Driver, method) is getattr(Operations, method):
            msg = f"Abstract driver operation: {adapter.name}/{method}"
            raise ValueError(msg)
    for feature, method in ((Feature.SILENT_SUBAGENT_NOTE, "silence"), (Feature.BACKGROUND_RESULT_WAKE, "wake")):
        if feature not in adapter.gaps and getattr(module.Driver, method) is getattr(Operations, method):
            msg = f"Unimplemented driver operation for supported capability: {adapter.name}/{method}"
            raise ValueError(msg)
    validate_decoder(Path(module.__file__))
    if problems := decoder_import_problems(Path(module.__file__)):
        msg = f"Decoder dependency imports production hooks/mapping: {problems}"
        raise ValueError(msg)
    validate_fixtures(adapter, module.Driver.fixtures)
    if not (harness.ROOT / "docs" / f"{adapter.name}.md").is_file():
        msg = f"Missing runtime documentation: {adapter.name}"
        raise ValueError(msg)


def validate_fixtures(adapter: Adapter, actions: Mapping[str, str]) -> None:
    """Require provenance-bearing recordings for semantic actions and registered events.

    Args:
        adapter: Runtime declaration.
        actions: Semantic action to recorded fixture name.

    Raises:
        ValueError: Recorded infrastructure is incomplete.
    """
    fixtures = harness.recorded(adapter.name)
    if not fixtures:
        msg = f"Missing recorded fixtures: {adapter.name}"
        raise ValueError(msg)
    mapping = importlib.import_module(adapter.mapping)
    registered = set(mapping.HOOK_EVENTS)
    missing = registered - {fixture.event for fixture in fixtures}
    if missing:
        msg = f"Missing registered-event fixtures: {adapter.name}: {sorted(missing)}"
        raise ValueError(msg)

    sources = {fixture.name: fixture for fixture in fixtures}
    for action, name in actions.items():
        if name not in sources or not sources[name].source:
            msg = f"Missing recorded action fixture/provenance: {adapter.name}/{action}/{name}"
            raise ValueError(msg)
    required_actions = {"prompt", "tool", "stop", "spawn", "write", "shell_write", "leaf", "start", "resume"}
    if Feature.COMPACTION_RECOVERY not in adapter.gaps:
        required_actions.add("compact")
    if missing_actions := required_actions - set(actions):
        msg = f"Missing semantic fixture actions: {adapter.name}: {sorted(missing_actions)}"
        raise ValueError(msg)


def validate(adapters: Mapping[str, Adapter] | None = None) -> Mapping[str, Adapter]:
    """Validate all infrastructure, including adapters with declared feature gaps.

    Args:
        adapters: Explicit declarations for extension and negative tests, or discovered adapters.

    Returns:
        The validated runtime declarations.
    """
    declarations = discover() if adapters is None else adapters
    validate_scenarios()
    for runtime, adapter in declarations.items():
        validate_declaration(adapter, runtime)
        validate_adapter(adapter)
    return declarations


def suite(adapters: Mapping[str, Adapter] | None = None) -> unittest.TestSuite:
    """Build deterministic runtime/contract/scenario test identifiers.

    Args:
        adapters: Explicit validated declarations, or discovered adapters.

    Returns:
        Executable cases with only documented capability gaps skipped.
    """
    result = unittest.TestSuite()
    for runtime, adapter in sorted(validate(adapters).items()):
        for contract, names in SCENARIOS.items():
            for name in names:
                case_type = type(f"{runtime}.{contract.value}", (ConformanceCase,), {"adapter": adapter})
                case = case_type(f"test_{name}")
                gap = adapter.gaps.get(contract)
                if gap:
                    status = "NOT IMPLEMENTED" if gap.kind is GapKind.NOT_IMPLEMENTED else "GAP"
                    case = GapCase(case.id(), f"{status}: {gap.kind.value}: {gap.reason} ({gap.documentation})")
                result.addTest(case)
    return result


class GapCase(unittest.TestCase):
    """A documented capability disposition with a stable conformance identifier."""

    def __init__(self, identifier: str, reason: str) -> None:
        super().__init__("runTest")
        self.identifier = identifier
        self.reason = reason

    def id(self) -> str:
        """Return the original runtime/feature/scenario identifier."""
        return self.identifier

    def runTest(self) -> None:  # noqa: N802
        """Expose a documented gap without marking it as passing behavior."""
        self.skipTest(self.reason)
