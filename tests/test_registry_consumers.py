"""Runtime selection and generated support docs consume registry declarations."""

import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cli
from adapters.registry import discover
from core.features import Adapter
from core.kit import source_files
from scripts import gen_docs, record_payloads
from tests import harness


class RegistryConsumerTests(unittest.TestCase):
    """Check deterministic runtime selection and explicit native recording support."""

    def test_shipped_cli_discovers_metadata_without_source_docs_or_tests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for source in source_files(harness.ROOT):
                destination = target / source.relative_to(harness.ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            result = subprocess.run(
                [sys.executable, str(target / "cli.py"), "--help"],
                capture_output=True,
                text=True,
                cwd=target,
                env=harness.hook_env(target),
                timeout=10,
                check=False,
            )
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            self.assertIn("claude", result.stdout)
            self.assertFalse((target / "docs").exists())
            self.assertFalse((target / "tests").exists())

    def test_cli_all_uses_registry_order_and_deduplicates_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(cli, "dispatch", return_value=0) as dispatch:
            self.assertEqual(cli.main(["status", "--runtime", "all", "--home", directory]), 0)
            self.assertEqual(dispatch.call_args.args[1], tuple(discover()))
            self.assertEqual(cli.main(["status", "--runtime", "cursor", "--runtime", "cursor", "--home", directory]), 0)
            self.assertEqual(dispatch.call_args.args[1], ("cursor",))

    def test_generated_matrix_labels_declarations_and_links_gap_kinds(self) -> None:
        template = f"{gen_docs.SUPPORT_BEGIN}\nold\n{gen_docs.SUPPORT_END}"
        result = gen_docs.declared_support(template)
        self.assertIn("not test results", result)
        for adapter in discover().values():
            for gap in adapter.gaps.values():
                self.assertIn(f"[{gap.kind.value}]({gap.documentation})", result)
        self.assertIn("| Capability | " + " | ".join(discover()) + " |", result)
        self.assertEqual(gen_docs.declared_support(result), result)

    def test_doc_check_includes_readme_support_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text(f"{gen_docs.SUPPORT_BEGIN}\nstale\n{gen_docs.SUPPORT_END}")
            with (
                patch.object(gen_docs, "ROOT", root),
                patch.object(gen_docs, "RUNTIMES", ()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(gen_docs.main(["--check"]), 1)

    def test_new_runtime_needs_explicit_native_recorder_before_source_reads(self) -> None:
        runtimes = {"future": Adapter("future", "adapters.future.hooks", "adapters.future.mapping", {})}
        with (
            patch.object(record_payloads, "discover", return_value=runtimes),
            patch.object(record_payloads, "Scrubber") as scrubber,
            contextlib.redirect_stderr(io.StringIO()) as errors,
            self.assertRaises(SystemExit) as raised,
        ):
            record_payloads.main(["--runtime", "all", "--home", "/tmp/unused-recording-home"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("no native fixture recorder for: future", errors.getvalue())
        scrubber.assert_not_called()
