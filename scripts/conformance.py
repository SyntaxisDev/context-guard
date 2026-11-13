"""Run shared behavior conformance separately from structural completeness checks."""

import argparse
import json
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.registry import discover
from tests.conformance.catalogue import SCENARIOS
from tests.conformance.discovery import GapCase, suite, validate

STATUSES = ("PASS", "FAIL", "GAP", "NOT IMPLEMENTED")


class ConformanceResult(unittest.TextTestResult):
    """Report behavior results distinctly from documented capability dispositions."""

    def startTestRun(self) -> None:  # noqa: N802
        """Initialize the machine-readable scenario records."""
        super().startTestRun()
        self.records: list[dict[str, str]] = []

    def record(self, test: unittest.TestCase, status: str, reason: str = "", evidence: str = "") -> None:
        """Capture one scenario disposition and failure evidence.

        Args:
            test: Executed scenario.
            status: PASS, FAIL, GAP or NOT IMPLEMENTED.
            reason: Failure or gap explanation.
            evidence: Exception traceback when execution failed.
        """
        runtime, contract, scenario = identity(test)
        for existing in self.records:
            if (existing["runtime"], existing["contract"], existing["scenario"]) == (runtime, contract, scenario):
                if status == "FAIL":
                    existing["status"] = "FAIL"
                    existing["reason"] = "\n".join(value for value in (existing["reason"], reason) if value)
                    existing["evidence"] = "\n".join(value for value in (existing["evidence"], evidence) if value)
                return
        self.records.append(
            {
                "status": status,
                "runtime": runtime,
                "contract": contract,
                "scenario": scenario,
                "reason": reason,
                "evidence": evidence,
            }
        )

    def addSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        """Print an explicit passing behavior result.

        Args:
            test: Passing scenario.
        """
        self.record(test, "PASS")
        super().addSuccess(test)
        self.stream.writeln(f"PASS {test.id()}")

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:  # noqa: N802
        """Accept only declared capability gaps; reject other skips as failures.

        Args:
            test: Skipped scenario.
            reason: Skip explanation.
        """
        if isinstance(test, GapCase) and reason == test.reason and reason.startswith(("GAP:", "NOT IMPLEMENTED:")):
            status = reason.split(":", 1)[0]
            self.record(test, status, reason)
            super().addSkip(test, reason)
            self.stream.writeln(f"{status} {test.id()}: {reason}")
            return
        message = f"Unexpected skip: {reason}"
        self.record(test, "FAIL", message)
        self.failures.append((test, message))
        self.stream.writeln(f"FAIL {test.id()} ({message})")

    def addExpectedFailure(self, test: unittest.TestCase, err: tuple) -> None:  # noqa: N802
        """Reject an expected failure as a missing behavior guarantee.

        Args:
            test: Scenario with an expected failure.
            err: Assertion or execution exception.
        """
        super().addExpectedFailure(test, err)
        reason = f"Unexpected expected failure: {err[1]}"
        self.record(test, "FAIL", reason, self._exc_info_to_string(err, test))
        self.failures.append((test, reason))
        self.stream.writeln(f"FAIL {test.id()} (expected failure)")

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        """Reject an unexpected success as an unapproved test disposition.

        Args:
            test: Scenario marked as an expected failure that passed.
        """
        super().addUnexpectedSuccess(test)
        self.record(test, "FAIL", "Unexpected success of expected-failure scenario")
        self.stream.writeln(f"FAIL {test.id()} (unexpected success)")

    def addFailure(self, test: unittest.TestCase, err: tuple) -> None:  # noqa: N802
        """Print a behavior failure.

        Args:
            test: Failing scenario.
            err: The assertion exception.
        """
        self.record(test, "FAIL", str(err[1]), self._exc_info_to_string(err, test))
        super().addFailure(test, err)
        self.stream.writeln(f"FAIL {test.id()}")

    def addError(self, test: unittest.TestCase, err: tuple) -> None:  # noqa: N802
        """Print an infrastructure or execution failure.

        Args:
            test: Scenario that raised an unexpected error.
            err: The execution exception.
        """
        self.record(test, "FAIL", str(err[1]), self._exc_info_to_string(err, test))
        super().addError(test, err)
        self.stream.writeln(f"FAIL {test.id()} (execution error)")

    def addSubTest(self, test: unittest.TestCase, subtest: unittest.TestCase, err: tuple | None) -> None:  # noqa: N802
        """Aggregate subtest assertion failures and errors into their owning scenario.

        Args:
            test: Executed scenario owning the subtest.
            subtest: Subtest including its parameter values.
            err: Failure or execution exception, or None for a passing subtest.
        """
        super().addSubTest(test, subtest, err)
        if err is not None:
            reason = f"{subtest.id()}: {err[1]}"
            self.record(test, "FAIL", reason, self._exc_info_to_string(err, subtest))
            self.stream.writeln(f"FAIL {test.id()} ({subtest})")


def identity(test: unittest.TestCase) -> tuple[str, str, str]:
    """Extract the runtime, contract and inventory scenario from a test identifier.

    Args:
        test: Generated shared scenario.

    Returns:
        The runtime, contract and scenario names.
    """
    runtime, contract, method = test.id().split(".")[-3:]
    return runtime, contract, method.removeprefix("test_")


def arguments() -> argparse.Namespace:
    """Parse focused execution selectors against the discovered inventory.

    Returns:
        Validated command-line arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--structure", action="store_true", help="validate infrastructure without running behavior")
    parser.add_argument("--runtime", action="append", choices=tuple(discover()), help="runtime to execute; repeatable")
    parser.add_argument(
        "--contract",
        action="append",
        choices=tuple(contract.value for contract in SCENARIOS),
        help="contract to execute; repeatable",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=tuple(name for names in SCENARIOS.values() for name in names),
        help="scenario to execute; repeatable",
    )
    parser.add_argument("--json", type=Path, dest="report", help="write scenario results and summary counts as JSON")
    return parser.parse_args()


def selected(test: unittest.TestCase, args: argparse.Namespace) -> bool:
    """Apply execution selectors after complete infrastructure validation.

    Args:
        test: Generated shared scenario.
        args: Parsed selectors.

    Returns:
        Whether all supplied selector dimensions include the scenario.
    """
    runtime, contract, scenario = identity(test)
    return (
        (not args.runtime or runtime in args.runtime)
        and (not args.contract or contract in args.contract)
        and (not args.scenario or scenario in args.scenario)
    )


def write_report(path: Path, result: ConformanceResult) -> None:
    """Write machine-readable dispositions, evidence and deterministic summary counts.

    Args:
        path: Requested output file.
        result: Executed shared scenario results.
    """
    counts = Counter(record["status"] for record in result.records)
    summary = {status: counts[status] for status in STATUSES}
    path.write_text(json.dumps({"structure": "PASS", "summary": summary, "results": result.records}, indent=2) + "\n")


def reconciled(result: ConformanceResult, selected_cases: list[unittest.TestCase]) -> bool:
    """Require one allowed terminal record per selected scenario.

    Args:
        result: Completed conformance execution.
        selected_cases: Cases chosen before the suite is consumed.

    Returns:
        Whether executed cases, records and summary totals agree exactly.
    """
    expected = Counter(identity(case) for case in selected_cases)
    recorded = Counter((record["runtime"], record["contract"], record["scenario"]) for record in result.records)
    counts = Counter(record["status"] for record in result.records)
    summary = {status: counts[status] for status in STATUSES}
    return (
        result.testsRun == len(selected_cases)
        and all(count == 1 for count in expected.values())
        and recorded == expected
        and set(counts) <= set(STATUSES)
        and sum(summary.values()) == len(result.records)
    )


def main() -> int:
    """Validate structure or execute the complete offline conformance matrix.

    Returns:
        Zero when structure is complete or all runnable behavior conforms, one otherwise.
    """
    args = arguments()
    try:
        declarations = validate()
    except (ImportError, ValueError, AttributeError, OSError) as error:
        print(f"STRUCTURE FAIL: {error}", file=sys.stderr)
        return 1
    print(f"STRUCTURE PASS: {len(declarations)} adapters", flush=True)
    if args.structure:
        return 0
    selected_cases = [test for test in suite(declarations) if selected(test, args)]
    if not selected_cases:
        print("No scenarios match the supplied selectors", file=sys.stderr)
        return 1
    result = unittest.TextTestRunner(resultclass=ConformanceResult, verbosity=0).run(unittest.TestSuite(selected_cases))
    complete = reconciled(result, selected_cases)
    if not complete:
        print("CONFORMANCE FAIL: scenario records do not match selected cases", file=sys.stderr)
    if args.report:
        write_report(args.report, result)
    incomplete = any(record["status"] == "NOT IMPLEMENTED" for record in result.records)
    return int(
        not complete
        or not result.wasSuccessful()
        or incomplete
        or any(record["status"] == "FAIL" for record in result.records)
    )


if __name__ == "__main__":
    raise SystemExit(main())
