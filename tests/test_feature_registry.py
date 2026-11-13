"""Registry declarations cover runtime packages and point to documented gaps."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import adapters
from adapters import registry
from core.features import Adapter, Feature, Gap, GapKind, Guarantee

ROOT = Path(__file__).resolve().parents[1]


class FeatureRegistryTests(unittest.TestCase):
    """Check discovery and declared capability gaps."""

    def test_registered_adapters_match_runtime_packages(self) -> None:
        runtime_dirs = {p.name for p in (ROOT / "adapters").iterdir() if (p / "hooks.py").is_file()}
        found = registry.discover()
        self.assertEqual(set(found), runtime_dirs)
        self.assertEqual(list(found), sorted(runtime_dirs))

    def test_discovery_does_not_import_hooks_or_mappings(self) -> None:
        with patch.object(registry.importlib, "import_module", wraps=registry.importlib.import_module) as load:
            registry.discover()
        self.assertTrue(all(call.args[0].endswith(".adapter") for call in load.call_args_list))

    def test_gaps_link_to_runtime_documentation_sections(self) -> None:
        for adapter in registry.discover().values():
            for feature, gap in adapter.gaps.items():
                with self.subTest(runtime=adapter.name, feature=feature):
                    path, anchor = gap.documentation.split("#", 1)
                    self.assertEqual(path, f"docs/{adapter.name}.md")
                    headings = re.findall(r"^#{1,6} (.+)$", (ROOT / path).read_text(), re.MULTILINE)
                    anchors = {re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-") for heading in headings}
                    self.assertIn(anchor, anchors)

    def test_runtime_with_hooks_requires_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "new_runtime"
            runtime.mkdir()
            (runtime / "hooks.py").touch()
            with patch.object(adapters, "__path__", [directory]), self.assertRaisesRegex(ValueError, "Missing adapter"):
                registry.discover()

    def test_invalid_declaration_name_is_rejected(self) -> None:
        adapter = registry.discover()["cursor"]
        invalid = Adapter("other", adapter.hooks, adapter.mapping, {})
        with patch("adapters.cursor.adapter.ADAPTER", invalid), self.assertRaisesRegex(ValueError, "Invalid adapter"):
            registry.discover()

    def test_invalid_gaps_and_guarantee_opt_out_are_rejected(self) -> None:
        adapter = registry.discover()["cursor"]
        bad_gaps = (
            {Guarantee.HOOK_BOUNDARY: Gap(GapKind.RUNTIME_LIMIT, "reason", "doc")},
            {Feature.HANDOFF_ACCESS: Gap("invalid", "reason", "doc")},
            {Feature.HANDOFF_ACCESS: Gap(GapKind.RUNTIME_LIMIT, " ", "doc")},
        )
        for gaps in bad_gaps:
            with self.subTest(gaps=gaps):
                invalid = Adapter(adapter.name, adapter.hooks, adapter.mapping, gaps)
                with (
                    patch("adapters.cursor.adapter.ADAPTER", invalid),
                    self.assertRaisesRegex(ValueError, "feature gap"),
                ):
                    registry.discover()

    def test_marked_directory_without_hooks_is_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "marked"
            runtime.mkdir()
            (runtime / "adapter.py").touch()
            with (
                patch.object(adapters, "__path__", [directory]),
                self.assertRaisesRegex(ValueError, "Invalid adapter"),
            ):
                registry.discover()

    def test_missing_adapter_value_is_rejected(self) -> None:
        with patch("adapters.cursor.adapter.ADAPTER", None), self.assertRaisesRegex(ValueError, "Invalid adapter"):
            registry.discover()

    def test_missing_or_external_documentation_target_is_rejected(self) -> None:
        for link in ("docs/cursor.md#missing-anchor", "docs/codex.md#subagent-silence-notes", "../outside.md#anchor"):
            with self.subTest(link=link), self.assertRaises(ValueError):
                registry.validate_documentation("cursor", link)

    def test_unmarked_directories_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "unrelated").mkdir()
            with patch.object(adapters, "__path__", [directory]):
                self.assertEqual(registry.discover(), {})

    def test_runtime_documentation_is_required_without_feature_gaps(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(registry, "ROOT", Path(directory)),
            self.assertRaisesRegex(ValueError, "Missing runtime documentation"),
        ):
            registry.validate_adapter_documentation(
                Adapter("claude", "adapters.claude.hooks", "adapters.claude.mapping", {})
            )

    def test_unloadable_registration_reports_runtime(self) -> None:
        with (
            patch.object(registry.importlib, "import_module", side_effect=ImportError("missing dependency")),
            self.assertRaisesRegex(ValueError, "Cannot load adapter declaration: adapters.claude.adapter"),
        ):
            registry.discover()

    def test_no_universal_guarantee_accepts_any_gap_kind(self) -> None:
        adapter = registry.discover()["cursor"]
        for guarantee in Guarantee:
            for kind in GapKind:
                gaps = {guarantee: Gap(kind, "reason", "docs/cursor.md#subagent-silence-notes")}
                invalid = Adapter(adapter.name, adapter.hooks, adapter.mapping, gaps)
                with self.subTest(guarantee=guarantee, kind=kind), self.assertRaisesRegex(ValueError, "feature gap"):
                    registry.validate_declaration(invalid, adapter.name)
