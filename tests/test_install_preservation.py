"""Manifest-backed hook ownership when user settings change between install and uninstall."""

import importlib
import json
import tempfile
import unittest
from pathlib import Path

from core import install
from core.install_spec import InstallSpec

RUNTIMES = ("claude", "codex", "cursor", "genie")


class PreservationTests(unittest.TestCase):
    """Exercise native hook merges through the shared engine in disposable homes."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def spec(self, runtime: str) -> InstallSpec:
        """Build only the native hook merge, leaving unrelated installed files out.

        Args:
            runtime: Adapter whose hook registration is tested.

        Returns:
            The native JSON merge specification.
        """
        mapping = importlib.import_module(f"adapters.{runtime}.mapping")
        hooks = importlib.import_module(f"adapters.{runtime}.hooks")
        kit = self.home / ".context-guard" / "kit"
        config = hooks.load(self.home, {}) if runtime == "cursor" else hooks.load_config(self.home, environ={})
        native = mapping.install_spec(
            self.home,
            kit,
            kit / ".venv" / "bin" / "python",
            config,
            **({} if runtime == "cursor" else {"environ": {}}),
        )
        return InstallSpec(runtime, json_merges=native.json_merges)

    def start(self, runtime: str, original: dict) -> tuple[InstallSpec, Path, dict]:
        """Install hooks into an existing user document.

        Args:
            runtime: Adapter to install.
            original: User document to seed.

        Returns:
            The spec, hook file and installed manifest.
        """
        spec = self.spec(runtime)
        path = spec.json_merges[0].path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(original))
        plan = install.plan(spec, {})
        install.apply(plan, install.manifest_path(self.home, runtime), self.home / "backups")
        self.assertFalse(plan.conflicts)
        return spec, path, plan.manifest

    def finish(self, spec: InstallSpec, manifest: dict) -> None:
        """Apply uninstall and assert that no conflicts were hidden.

        Args:
            spec: Installed native merge.
            manifest: Ownership recorded by install.
        """
        plan = install.plan_uninstall(spec, manifest)
        self.assertFalse(plan.conflicts)
        install.apply(plan, install.manifest_path(self.home, spec.runtime), self.home / "backups")

    def test_original_empty_events_and_objects_survive_unrelated_user_edits(self) -> None:
        for runtime in RUNTIMES:
            event = "stop" if runtime == "cursor" else "Stop"
            for hooks in ({}, {event: []}, {event: [], "userEvent": []}):
                with self.subTest(runtime=runtime, hooks=hooks):
                    original = {"hooks": hooks, "theme": "dark"}
                    spec, path, manifest = self.start(runtime, original)
                    path.write_text(json.dumps(json.loads(path.read_text()) | {"font": 14}))
                    self.finish(spec, manifest)
                    self.assertEqual(json.loads(path.read_text()), original | {"font": 14})

    def test_user_deleted_event_is_not_resurrected(self) -> None:
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                event = "stop" if runtime == "cursor" else "Stop"
                spec, path, manifest = self.start(runtime, {"hooks": {event: []}, "theme": "dark"})
                current = json.loads(path.read_text())
                del current["hooks"][event]
                path.write_text(json.dumps(current))
                self.finish(spec, manifest)
                self.assertEqual(json.loads(path.read_text()), {"theme": "dark"})

    def test_cursor_preserves_user_versions_and_removes_its_added_default(self) -> None:
        for original in ({}, {"version": 2}, {"version": 2, "hooks": {}}):
            with self.subTest(original=original):
                spec, path, manifest = self.start("cursor", original)
                self.assertEqual(json.loads(path.read_text())["version"], original.get("version", 1))
                path.write_text(json.dumps(json.loads(path.read_text()) | {"font": 14}))
                self.finish(spec, manifest)
                self.assertEqual(json.loads(path.read_text()), original | {"font": 14})

    def test_cursor_keeps_an_intervening_version_change(self) -> None:
        spec, path, manifest = self.start("cursor", {"theme": "dark"})
        path.write_text(json.dumps(json.loads(path.read_text()) | {"version": 3}))
        self.finish(spec, manifest)
        self.assertEqual(json.loads(path.read_text()), {"theme": "dark", "version": 3})

    def test_cursor_keeps_user_version_after_manual_hook_removal(self) -> None:
        for version in (1, 3):
            with self.subTest(version=version):
                spec, path, manifest = self.start("cursor", {"version": version})
                path.write_text(json.dumps({"version": version}))
                self.finish(spec, manifest)
                self.assertEqual(json.loads(path.read_text()), {"version": version})

    def test_unedited_roundtrip_retains_original_line_endings(self) -> None:
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                spec = self.spec(runtime)
                path = spec.json_merges[0].path
                path.parent.mkdir(parents=True, exist_ok=True)
                event = "stop" if runtime == "cursor" else "Stop"
                original = json.dumps({"hooks": {event: []}}, indent=3).replace("\n", "\r\n").encode()
                path.write_bytes(original)
                plan = install.plan(spec, {})
                install.apply(plan, install.manifest_path(self.home, runtime), self.home / "backups")
                self.finish(spec, plan.manifest)
                self.assertEqual(path.read_bytes(), original)
