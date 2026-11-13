"""Completeness checks independent of current adapter behavior conformance."""

import importlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from adapters.registry import discover
from core.features import Feature, Gap, GapKind, Guarantee
from tests import harness
from tests.conformance.catalogue import SCENARIOS
from tests.conformance.discovery import validate, validate_decoder, validate_fixtures, validate_scenarios
from tests.driver_support import Operations


class ConformanceStructureTests(unittest.TestCase):
    """Missing infrastructure cannot masquerade as unsupported capabilities."""

    def test_all_registered_adapters_have_complete_infrastructure(self) -> None:
        self.assertTrue(validate())

    def test_every_contract_has_executable_constituent_scenarios(self) -> None:
        validate_scenarios()
        self.assertEqual(set(SCENARIOS), set(Feature) | set(Guarantee))

    def test_missing_contract_fails_even_when_all_adapters_declare_a_gap(self) -> None:
        scenarios = dict(SCENARIOS)
        del scenarios[Feature.COMPACTION_RECOVERY]
        with self.assertRaisesRegex(ValueError, "missing"):
            validate_scenarios(scenarios)

    def test_unknown_contract_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown"):
            validate_scenarios({**SCENARIOS, "unknown": ("prompt_warning_once",)})

    def test_missing_executable_scenario_fails(self) -> None:
        scenarios = {**SCENARIOS, Feature.PROMPT_WARNING: ("does_not_exist",)}
        with self.assertRaisesRegex(ValueError, "Missing executable"):
            validate_scenarios(scenarios)

    def test_missing_recorded_fixtures_fails_completeness(self) -> None:
        with (
            mock.patch("tests.harness.recorded", return_value=[]),
            self.assertRaisesRegex(ValueError, "Missing recorded fixtures"),
        ):
            validate()

    def test_missing_driver_operation_fails_completeness(self) -> None:

        adapter = next(iter(discover().values()))
        module = importlib.import_module(f"adapters.{adapter.name}.tests.driver")
        with (
            mock.patch.object(module.Driver, "fire", None),
            self.assertRaisesRegex(ValueError, "Missing driver operation"),
        ):
            validate({adapter.name: adapter})

    def test_missing_registered_event_fixture_fails_completeness(self) -> None:

        adapter = next(iter(discover().values()))
        fixtures = harness.recorded(adapter.name)
        event = next(iter(importlib.import_module(adapter.mapping).HOOK_EVENTS))
        with (
            mock.patch("tests.harness.recorded", return_value=[case for case in fixtures if case.event != event]),
            self.assertRaisesRegex(ValueError, "Missing registered-event fixtures"),
        ):
            validate({adapter.name: adapter})

    def test_inherited_abstract_driver_operation_fails_completeness(self) -> None:
        adapter = next(iter(discover().values()))
        module = importlib.import_module(f"adapters.{adapter.name}.tests.driver")
        with (
            mock.patch.object(module.Driver, "decode", Operations.decode),
            self.assertRaisesRegex(ValueError, "Abstract driver operation"),
        ):
            validate({adapter.name: adapter})

    def test_missing_semantic_action_fixture_fails_completeness(self) -> None:
        adapter = next(iter(discover().values()))
        module = importlib.import_module(f"adapters.{adapter.name}.tests.driver")
        actions = {key: value for key, value in module.Driver.fixtures.items() if key != "write"}
        with self.assertRaisesRegex(ValueError, "Missing semantic fixture actions"):
            validate_fixtures(adapter, actions)

    def test_supported_capability_cannot_inherit_a_gap_operation(self) -> None:
        for feature, method in ((Feature.SILENT_SUBAGENT_NOTE, "silence"), (Feature.BACKGROUND_RESULT_WAKE, "wake")):
            for adapter in discover().values():
                if feature in adapter.gaps:
                    continue
                module = importlib.import_module(f"adapters.{adapter.name}.tests.driver")
                with (
                    self.subTest(runtime=adapter.name, method=method),
                    mock.patch.object(module.Driver, method, getattr(Operations, method)),
                    self.assertRaisesRegex(ValueError, "Unimplemented driver operation"),
                ):
                    validate({adapter.name: adapter})

    def test_missing_named_fixture_fails_even_when_registered_events_are_covered(self) -> None:
        adapter = next(iter(discover().values()))
        module = importlib.import_module(f"adapters.{adapter.name}.tests.driver")
        actions = {**module.Driver.fixtures, "write": "missing-recording"}
        with self.assertRaisesRegex(ValueError, "Missing recorded action fixture/provenance"):
            validate_fixtures(adapter, actions)

    def test_decoder_import_alias_cannot_delegate_to_production(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decoder.py"
            for source in (
                "import adapters.codex.hooks as hidden\n",
                "from adapters.codex import mapping as hidden\n",
                "from adapters.codex.hooks import respond as decode\n",
            ):
                path.write_text(source)
                with self.assertRaisesRegex(ValueError, "production hooks/mapping"):
                    validate_decoder(path)

    def test_duplicate_scenario_registration_fails(self) -> None:
        scenarios = {**SCENARIOS, Feature.PROMPT_BLOCK: ("prompt_warning_once",)}
        with self.assertRaisesRegex(ValueError, "Duplicate shared"):
            validate_scenarios(scenarios)

    def test_unregistered_executable_scenario_fails(self) -> None:
        scenarios = {**SCENARIOS, Feature.PROMPT_WARNING: ("prompt_warning_once",)}
        with self.assertRaisesRegex(ValueError, "Unregistered executable"):
            validate_scenarios(scenarios)

    def test_empty_contract_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "Missing scenarios"):
            validate_scenarios({**SCENARIOS, Feature.PROMPT_WARNING: ()})

    def test_missing_driver_fails_even_when_all_capabilities_declare_gaps(self) -> None:
        adapter = next(iter(discover().values()))
        adapter = replace(
            adapter,
            gaps={feature: Gap(GapKind.NOT_IMPLEMENTED, "pending", "docs/unused.md#gap") for feature in Feature},
        )
        original = importlib.import_module

        def without_driver(name: str) -> object:
            """Simulate missing native infrastructure without changing source files.

            Args:
                name: Requested module import.

            Returns:
                The original imported module for other names.

            Raises:
                ModuleNotFoundError: The selected adapter has no driver.
            """
            if name == f"adapters.{adapter.name}.tests.driver":
                msg = f"No module named {name}"
                raise ModuleNotFoundError(msg)
            return original(name)

        with (
            mock.patch("tests.conformance.discovery.validate_documentation"),
            mock.patch("tests.conformance.discovery.importlib.import_module", side_effect=without_driver),
            self.assertRaisesRegex(ModuleNotFoundError, "tests.driver"),
        ):
            validate({adapter.name: adapter})
