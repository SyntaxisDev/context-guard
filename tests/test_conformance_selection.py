"""Discovery entry points and report selectors retain complete contract identities."""

import argparse
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from adapters.registry import discover
from core.features import Feature, Gap, GapKind, Guarantee
from scripts import conformance
from tests.conformance import discovery
from tests.conformance.catalogue import SCENARIOS


class ConformanceSelectionTests(unittest.TestCase):
    """Filters select executions without hiding infrastructure or classifying gaps as passes."""

    def test_unapproved_dispositions_fail_and_have_complete_reports(self) -> None:
        class Skipped(unittest.TestCase):
            """Unexpectedly skip a required guarantee."""

            def id(self) -> str:
                return "shared.codex.hook_boundary.test_boundary_deadline"

            def runTest(self) -> None:  # noqa: N802
                self.skipTest("native evidence unavailable")

        class ExpectedFailure(unittest.TestCase):
            """Mask a broken guarantee with unittest's expected failure."""

            def id(self) -> str:
                return "shared.codex.hook_boundary.test_boundary_deadline"

            @unittest.expectedFailure
            def runTest(self) -> None:  # noqa: N802
                self.fail("boundary broken")

        class UnexpectedSuccess(ExpectedFailure):
            """Pass a scenario marked as expected to fail."""

            @unittest.expectedFailure
            def runTest(self) -> None:  # noqa: N802
                pass

        for scenario in (Skipped, ExpectedFailure, UnexpectedSuccess):
            with self.subTest(scenario=scenario.__name__), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report.json"
                selectors = argparse.Namespace(runtime=None, contract=None, scenario=None, structure=False, report=path)
                with (
                    mock.patch.object(conformance, "arguments", return_value=selectors),
                    mock.patch.object(conformance, "validate", return_value={}),
                    mock.patch.object(conformance, "suite", return_value=unittest.TestSuite([scenario()])),
                    mock.patch("sys.stdout", new_callable=io.StringIO),
                    mock.patch("sys.stderr", new_callable=io.StringIO),
                ):
                    self.assertEqual(conformance.main(), 1)
                report = json.loads(path.read_text())
                self.assertEqual(report["summary"], {"PASS": 0, "FAIL": 1, "GAP": 0, "NOT IMPLEMENTED": 0})
                self.assertEqual(len(report["results"]), 1)
                self.assertEqual(report["results"][0]["status"], "FAIL")

    def test_package_module_and_directory_loading_never_run_unbound_cases(self) -> None:
        for name in ("tests.conformance", "tests.conformance.test_cases"):
            with self.subTest(name=name):
                loader = unittest.TestLoader()
                self.assertEqual(loader.loadTestsFromName(name).countTestCases(), 0)
                self.assertEqual(loader.errors, [])
        loader = unittest.TestLoader()
        self.assertEqual(loader.discover("tests/conformance", top_level_dir=".").countTestCases(), 0)
        self.assertEqual(loader.errors, [])

    def test_catalogue_order_and_ids_cover_every_discovered_adapter_once(self) -> None:
        adapters = discover()
        with mock.patch.object(discovery, "validate", return_value=adapters):
            cases = list(discovery.suite())
        expected = [
            (runtime, contract.value, name)
            for runtime in sorted(adapters)
            for contract, names in SCENARIOS.items()
            for name in names
        ]
        self.assertEqual([conformance.identity(case) for case in cases], expected)
        self.assertEqual(len({case.id() for case in cases}), len(expected))

    def test_all_gap_kinds_only_replace_feature_scenarios(self) -> None:
        adapter = next(iter(discover().values()))
        for kind in GapKind:
            declared = replace(adapter, gaps={Feature.PROMPT_WARNING: Gap(kind, "evidence", "docs/runtime.md#gap")})
            with (
                self.subTest(kind=kind),
                mock.patch.object(discovery, "validate", return_value={adapter.name: declared}),
            ):
                cases = list(discovery.suite())
                gaps = [case for case in cases if isinstance(case, discovery.GapCase)]
                self.assertEqual(len(gaps), len(SCENARIOS[Feature.PROMPT_WARNING]))
                result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=conformance.ConformanceResult).run(
                    unittest.TestSuite(gaps)
                )
                status = "NOT IMPLEMENTED" if kind is GapKind.NOT_IMPLEMENTED else "GAP"
                self.assertEqual({record["status"] for record in result.records}, {status})
                guarantees = {contract.value for contract in Guarantee}
                self.assertFalse(any(conformance.identity(case)[1] in guarantees for case in gaps))

    def test_selectors_intersect_and_report_only_executed_cases(self) -> None:
        adapter = next(iter(discover().values()))
        declared = replace(
            adapter, gaps={Feature.PROMPT_WARNING: Gap(GapKind.NOT_IMPLEMENTED, "pending", "docs/r.md#g")}
        )
        selectors = argparse.Namespace(
            runtime=[adapter.name], contract=["prompt_warning"], scenario=["prompt_warning_once"]
        )
        with mock.patch.object(discovery, "validate", return_value={adapter.name: declared}):
            cases = list(discovery.suite())
        chosen = [case for case in cases if conformance.selected(case, selectors)]
        self.assertEqual(len(chosen), 1)
        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=conformance.ConformanceResult).run(
            unittest.TestSuite(chosen)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            conformance.write_report(path, result)
            report = json.loads(path.read_text())
        self.assertEqual(report["summary"], {"PASS": 0, "FAIL": 0, "GAP": 0, "NOT IMPLEMENTED": 1})
        self.assertEqual(report["results"][0]["scenario"], "prompt_warning_once")
        selectors.runtime = ["unselected"]
        self.assertFalse(any(conformance.selected(case, selectors) for case in cases))

    def test_filter_does_not_hide_structure_failure(self) -> None:
        selectors = argparse.Namespace(runtime=["absent"], contract=None, scenario=None, structure=False, report=None)
        with (
            mock.patch.object(conformance, "arguments", return_value=selectors),
            mock.patch.object(conformance, "validate", side_effect=ValueError("missing native driver")),
            mock.patch.object(conformance, "suite") as build,
            mock.patch("sys.stderr", new_callable=io.StringIO) as errors,
        ):
            self.assertEqual(conformance.main(), 1)
        self.assertIn("STRUCTURE FAIL", errors.getvalue())
        build.assert_not_called()

    def test_cli_exit_status_distinguishes_unfinished_from_runtime_gaps(self) -> None:
        adapter = next(iter(discover().values()))
        selectors = argparse.Namespace(runtime=None, contract=None, scenario=None, structure=False, report=None)
        for kind in GapKind:
            status = "NOT IMPLEMENTED" if kind is GapKind.NOT_IMPLEMENTED else "GAP"
            case = discovery.GapCase(
                f"shared.{adapter.name}.prompt_warning.test_prompt_warning_once", f"{status}: reason"
            )
            with (
                self.subTest(kind=kind),
                mock.patch.object(conformance, "arguments", return_value=selectors),
                mock.patch.object(conformance, "validate", return_value={adapter.name: adapter}),
                mock.patch.object(conformance, "suite", return_value=unittest.TestSuite([case])),
                mock.patch("sys.stdout", new_callable=io.StringIO),
                mock.patch("sys.stderr", new_callable=io.StringIO),
            ):
                self.assertEqual(conformance.main(), int(kind is GapKind.NOT_IMPLEMENTED))

    def test_cli_rejects_empty_filter_intersection(self) -> None:
        selectors = argparse.Namespace(
            runtime=["unselected"], contract=None, scenario=None, structure=False, report=None
        )
        case = discovery.GapCase("shared.example.prompt_warning.test_prompt_warning_once", "GAP: evidence")
        with (
            mock.patch.object(conformance, "arguments", return_value=selectors),
            mock.patch.object(conformance, "validate", return_value={}),
            mock.patch.object(conformance, "suite", return_value=unittest.TestSuite([case])),
            mock.patch("sys.stdout", new_callable=io.StringIO),
            mock.patch("sys.stderr", new_callable=io.StringIO) as errors,
        ):
            self.assertEqual(conformance.main(), 1)
        self.assertIn("No scenarios match", errors.getvalue())

    def test_subtest_failures_and_errors_are_one_reported_cli_failure(self) -> None:
        class Scenario(unittest.TestCase):
            """Expose assertion and execution failures within one selected scenario."""

            def id(self) -> str:
                return "shared.example.prompt_block.test_handback_delivery_above_block"

            def runTest(self) -> None:  # noqa: N802
                for value in ("blocked", "exception", "pass"):
                    with self.subTest(prompt=value):
                        if value == "blocked":
                            self.fail("report was blocked")
                        if value == "exception":
                            msg = "native response missing"
                            raise ValueError(msg)

        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "subtests.json"
            selectors = argparse.Namespace(
                runtime=None, contract=None, scenario=None, structure=False, report=report_path
            )
            with (
                mock.patch.object(conformance, "arguments", return_value=selectors),
                mock.patch.object(conformance, "validate", return_value={}),
                mock.patch.object(conformance, "suite", return_value=unittest.TestSuite([Scenario()])),
                mock.patch("sys.stdout", new_callable=io.StringIO),
                mock.patch("sys.stderr", new_callable=io.StringIO),
            ):
                self.assertEqual(conformance.main(), 1)
            report = json.loads(report_path.read_text())
        self.assertEqual(report["summary"], {"PASS": 0, "FAIL": 1, "GAP": 0, "NOT IMPLEMENTED": 0})
        self.assertEqual(len(report["results"]), 1)
        record = report["results"][0]
        self.assertEqual(record["scenario"], "handback_delivery_above_block")
        self.assertIn("prompt='blocked'", record["reason"])
        self.assertIn("prompt='exception'", record["reason"])
        self.assertIn("report was blocked", record["evidence"])
        self.assertIn("native response missing", record["evidence"])

    def test_passing_subtests_produce_one_success_record(self) -> None:
        class Scenario(unittest.TestCase):
            """Expose multiple successful subtests in one selected scenario."""

            def id(self) -> str:
                return "shared.example.prompt_block.test_quoted_handback_still_blocks"

            def runTest(self) -> None:  # noqa: N802
                for value in range(3):
                    with self.subTest(value=value):
                        self.assertGreaterEqual(value, 0)

        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=conformance.ConformanceResult).run(
            unittest.TestSuite([Scenario()])
        )
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0]["status"], "PASS")
