"""Workspace permission setup preserves native ownership and profile boundaries."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from adapters.copilot import access, hooks, plugin
from core import package
from tests import harness


class AccessTests(unittest.TestCase):
    """Public helper behavior uses only temporary existing directories."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.workspace = self.home / "project"
        self.workspace.mkdir()
        self.root = self.home / ".copilot"
        self.root.mkdir()
        self.store = self.home / ".context-guard/handoffs"
        self.store.mkdir(parents=True)
        self.path = self.root / "permissions-config.json"

    def run_helper(self, command: str, *args: str, workspace: Path | None = None, env: dict | None = None) -> int:
        """Invoke the public helper without using the process environment.

        Args:
            command: Install or uninstall.
            *args: Additional CLI arguments.
            workspace: Workspace override.
            env: Isolated configuration environment.

        Returns:
            The helper exit status.
        """
        return access.main(
            [command, "--home", str(self.home), "--workspace", str(workspace or self.workspace), *args],
            env or {},
            io.StringIO(),
        )

    def snapshot(self) -> dict[Path, bytes]:
        """Capture every file byte for dry-run comparisons.

        Returns:
            Relative file paths mapped to their bytes.
        """
        return {p.relative_to(self.home): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}

    def test_roundtrip_restores_bytes_and_preserves_existing_matching_grants(self) -> None:
        for data in (
            {},
            {"locations": {}},
            {"locations": {str(self.workspace): {}}},
            {"locations": {str(self.workspace): {"allowed_directories": [str(self.store)]}}},
            {"locations": {"/foreign": {"allowed_directories": ["/foreign/store"]}}, "other": True},
        ):
            with self.subTest(data=data):
                original = json.dumps(data, indent=4) + "\n\n"
                self.path.write_text(original)
                for command in ("install", "install", "uninstall"):
                    self.assertEqual(self.run_helper(command), 0)
                self.assertEqual(self.path.read_text(), original)

    def test_dryrun_install_and_uninstall_write_nothing(self) -> None:
        for installed in (False, True):
            if installed:
                self.assertEqual(self.run_helper("install"), 0)
            before = self.snapshot()
            for command in ("install", "uninstall"):
                self.assertEqual(self.run_helper(command, "--dry-run"), 0)
                self.assertEqual(self.snapshot(), before)

    def test_two_workspaces_survive_either_uninstall_order(self) -> None:
        other = self.home / "other"
        other.mkdir()
        for first, last in ((self.workspace, other), (other, self.workspace)):
            for workspace in (self.workspace, other):
                self.assertEqual(self.run_helper("install", workspace=workspace), 0)
            self.assertEqual(self.run_helper("uninstall", workspace=first), 0)
            data = json.loads(self.path.read_text())
            self.assertEqual(data["locations"], {str(last): {"allowed_directories": [str(self.store)]}})
            self.assertEqual(self.run_helper("uninstall", workspace=last), 0)
            self.assertFalse(self.path.exists())

    def test_user_edits_and_foreign_entries_survive(self) -> None:
        self.assertEqual(self.run_helper("install"), 0)
        data = json.loads(self.path.read_text())
        location = data["locations"][str(self.workspace)]
        location["allowed_directories"] = ["/edited", "/foreign"]
        location["tool_approvals"] = ["user-owned"]
        data["locations"]["/other"] = {"custom": True}
        self.path.write_text(json.dumps(data))
        edited = self.path.read_bytes()
        self.assertEqual(self.run_helper("uninstall"), 0)
        self.assertEqual(self.path.read_bytes(), edited)

    def test_appended_user_directory_survives_retirement(self) -> None:
        self.assertEqual(self.run_helper("install"), 0)
        data = json.loads(self.path.read_text())
        data["locations"][str(self.workspace)]["allowed_directories"].append("/user")
        self.path.write_text(json.dumps(data))
        self.assertEqual(self.run_helper("uninstall"), 0)
        self.assertEqual(
            json.loads(self.path.read_text())["locations"][str(self.workspace)], {"allowed_directories": ["/user"]}
        )

    def test_native_profile_and_canonical_workspace_are_independent(self) -> None:
        profile = self.home / "profile"
        profile.mkdir()
        alias = self.home / "alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        env = {"COPILOT_HOME": str(profile)}
        self.assertEqual(self.run_helper("install", workspace=alias, env=env), 0)
        self.assertEqual(self.run_helper("install"), 0)
        self.assertEqual(self.run_helper("uninstall", env=env), 0)
        self.assertFalse((profile / "permissions-config.json").exists())
        self.assertEqual(set(json.loads(self.path.read_text())["locations"]), {str(self.workspace)})
        self.assertEqual(self.run_helper("uninstall", workspace=alias), 0)

    def test_configured_store_and_environment_override(self) -> None:
        custom = self.home / "custom"
        custom.mkdir()
        env = {"CONTEXT_GUARD_PATHS_HANDOFF_DIR": str(custom)}
        self.assertEqual(self.run_helper("install", env=env), 0)
        self.assertEqual(
            json.loads(self.path.read_text())["locations"][str(self.workspace)], {"allowed_directories": [str(custom)]}
        )
        self.assertEqual(self.run_helper("uninstall"), 0)
        self.assertFalse(self.path.exists())

    def test_invalid_native_containers_fail_without_permission_writes(self) -> None:
        for data in (
            {"locations": []},
            {"locations": {str(self.workspace): []}},
            {"locations": {str(self.workspace): {"allowed_directories": "bad"}}},
        ):
            self.path.write_text(json.dumps(data))
            before = self.path.read_bytes()
            self.assertEqual(self.run_helper("install"), 1)
            self.assertEqual(self.path.read_bytes(), before)

    def test_missing_directories_and_relative_native_profile_fail(self) -> None:
        for env in (
            {"COPILOT_HOME": "relative"},
            {"COPILOT_HOME": str(self.home / "missing")},
            {"CONTEXT_GUARD_PATHS_HANDOFF_DIR": str(self.home / "missing")},
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.run_helper("install", env=env), 2)
            self.assertFalse(self.path.exists())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_helper("install", workspace=self.home / "missing"), 2)

    def test_packaged_launcher_executes_public_helper(self) -> None:
        output = self.home / "package"
        spec = plugin.plugin_spec(hooks.load_config(self.home, {}))
        package.write(output, spec, package.plan(harness.ROOT, "copilot", spec))
        output = package.plugin_dir(output, spec)
        env = harness.hook_env(self.home)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
        result = subprocess.run(
            [
                "sh",
                str(output / package.LAUNCHER),
                "adapters/copilot/access.py",
                "install",
                "--home",
                str(self.home),
                "--workspace",
                str(self.workspace),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(self.path.read_text())["locations"][str(self.workspace)],
            {"allowed_directories": [str(self.store)]},
        )

    def test_malformed_uninstall_preserves_native_bytes_and_ownership_for_retry(self) -> None:
        workspace = str(self.workspace)
        for malformed in (
            {"locations": None},
            {"locations": []},
            {"locations": {workspace: None}},
            {"locations": {workspace: []}},
            {"locations": {workspace: {"allowed_directories": "broken"}}},
            {"locations": {workspace: {"allowed_directories": [1]}}},
        ):
            with self.subTest(malformed=malformed):
                self.path.write_text("{}")
                self.assertEqual(self.run_helper("install"), 0)
                manifests = {p: p.read_bytes() for p in self.home.rglob("install-manifest.json")}
                self.assertEqual(len(manifests), 1)
                self.path.write_text(json.dumps(malformed))
                original = self.path.read_bytes()
                self.assertEqual(self.run_helper("uninstall"), 1)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual({p: p.read_bytes() for p in manifests}, manifests)
                self.path.write_text(json.dumps({"locations": {workspace: {"allowed_directories": [str(self.store)]}}}))
                self.assertEqual(self.run_helper("uninstall"), 0)
                self.assertFalse(any(p.exists() for p in manifests))
