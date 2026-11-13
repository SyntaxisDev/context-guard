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

from adapters.cursor import access, hooks, plugin
from adapters.cursor.tests.driver import Driver
from core import package
from tests import harness
from tests.conformance.test_installation import InstallationContracts


class AccessTests(unittest.TestCase):
    """Public helper behavior uses only temporary existing directories."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.workspace = self.home / "project"
        self.workspace.mkdir()
        self.root = self.workspace / ".cursor"
        self.root.mkdir()
        self.store = self.home / ".context-guard/handoffs"
        self.store.mkdir(parents=True)
        self.path = self.root / "cli.json"
        self.grant = f"Write({self.store}/**)"

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

    def snapshot(self) -> dict[Path, bytes | None]:
        """Capture directories and every file byte for dry-run comparisons.

        Returns:
            Relative paths mapped to file bytes or None for directories.
        """
        return {p.relative_to(self.home): p.read_bytes() if p.is_file() else None for p in self.home.rglob("*")}

    def test_roundtrip_restores_bytes_and_preserves_existing_matching_grants(self) -> None:
        for data in (
            {},
            {"permissions": {}},
            {"permissions": {"allow": []}},
            {"permissions": {"allow": [self.grant]}},
            {"permissions": {"allow": ["Read(*)"], "deny": ["Shell(*)", "Write(*.key)"]}, "other": True},
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
            remaining = last / ".cursor/cli.json"
            self.assertEqual(json.loads(remaining.read_text())["permissions"]["allow"], [self.grant])
            self.assertFalse((first / ".cursor/cli.json").exists())
            self.assertEqual(self.run_helper("uninstall", workspace=last), 0)
            self.assertFalse(remaining.exists())

    def test_user_edits_and_foreign_entries_survive(self) -> None:
        self.assertEqual(self.run_helper("install"), 0)
        data = {"permissions": {"allow": ["Write(/edited/**)", "Read(*)"], "deny": ["Shell(*)"]}, "other": {}}
        self.path.write_text(json.dumps(data))
        edited = self.path.read_bytes()
        self.assertEqual(self.run_helper("uninstall"), 0)
        self.assertEqual(self.path.read_bytes(), edited)

    def test_appended_user_permission_and_empty_containers_survive_retirement(self) -> None:
        self.assertEqual(self.run_helper("install"), 0)
        data = json.loads(self.path.read_text())
        data["permissions"]["allow"].append("Read(*)")
        data["permissions"]["deny"] = []
        data["other"] = {}
        self.path.write_text(json.dumps(data))
        self.assertEqual(self.run_helper("uninstall"), 0)
        self.assertEqual(
            json.loads(self.path.read_text()), {"permissions": {"allow": ["Read(*)"], "deny": []}, "other": {}}
        )

    def test_native_profile_override_does_not_relocate_project_config_and_alias_is_canonical(self) -> None:
        alias = self.home / "alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        env = {"CURSOR_CONFIG_DIR": str(self.home / "unused-profile")}
        self.assertEqual(self.run_helper("install", workspace=alias, env=env), 0)
        self.assertEqual(self.run_helper("install"), 0)
        self.assertEqual(json.loads(self.path.read_text())["permissions"]["allow"], [self.grant])
        self.assertEqual(self.run_helper("uninstall", workspace=alias), 0)
        self.assertFalse(self.path.exists())
        self.assertFalse((self.home / "unused-profile").exists())

    def test_configured_store_and_environment_override(self) -> None:
        custom = self.home / "custom"
        custom.mkdir()
        env = {"CONTEXT_GUARD_PATHS_HANDOFF_DIR": str(custom)}
        self.assertEqual(self.run_helper("install", env=env), 0)
        self.assertEqual(json.loads(self.path.read_text())["permissions"]["allow"], [f"Write({custom}/**)"])
        self.assertEqual(self.run_helper("uninstall"), 0)
        self.assertFalse(self.path.exists())

    def test_invalid_native_containers_fail_without_permission_writes(self) -> None:
        for data in (
            {"permissions": []},
            {"permissions": {"allow": "bad"}},
            {"permissions": {"allow": [42]}},
            {"permissions": {"deny": "bad"}},
            {"permissions": {"deny": [42]}},
        ):
            self.path.write_text(json.dumps(data))
            before = self.path.read_bytes()
            self.assertEqual(self.run_helper("install"), 1)
            self.assertEqual(self.path.read_bytes(), before)

    def test_missing_configured_directories_fail(self) -> None:
        for env in ({"CONTEXT_GUARD_PATHS_HANDOFF_DIR": str(self.home / "missing")},):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.run_helper("install", env=env), 2)
            self.assertFalse(self.path.exists())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_helper("install", workspace=self.home / "missing"), 2)

    def test_packaged_launcher_executes_public_helper(self) -> None:
        output = self.home / "package"
        spec = plugin.plugin_spec(hooks.load(self.home, {}))
        package.write(output, spec, package.plan(harness.ROOT, "cursor", spec))
        output = package.plugin_dir(output, spec)
        env = harness.hook_env(self.home)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
        result = subprocess.run(
            [
                "sh",
                str(output / package.LAUNCHER),
                "adapters/cursor/access.py",
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
            json.loads(self.path.read_text())["permissions"]["allow"],
            [self.grant],
        )

    def test_driver_mixin_composes_with_current_driver_and_uses_project_permissions(self) -> None:
        driver = Driver(self.home, self.workspace, "access-contract")
        driver.configure_access()
        self.assertEqual(driver.access_grants(), {str(self.store)})
        data = json.loads(self.path.read_text())
        data["permissions"]["allow"] += ["Read(/foreign/**)", "Write(/foreign/file)"]
        self.path.write_text(json.dumps(data))
        self.assertEqual(driver.access_grants(), {str(self.store)})

    def test_conflicting_denies_are_reported_and_retained_without_shell_grants(self) -> None:
        denies = ["Write(*)", "Write(*.key)", f"Write({self.store}/**)", "Shell(*)"]
        self.path.write_text(json.dumps({"permissions": {"deny": denies}}))
        output = io.StringIO()
        code = access.main(["install", "--home", str(self.home), "--workspace", str(self.workspace)], {}, output)
        self.assertEqual(code, 0)
        self.assertIn("conflicting deny", output.getvalue())
        self.assertIn("readonly", output.getvalue())
        self.assertEqual(json.loads(self.path.read_text()), {"permissions": {"allow": [self.grant], "deny": denies}})
        self.assertEqual(self.run_helper("uninstall"), 0)
        self.assertEqual(json.loads(self.path.read_text()), {"permissions": {"deny": denies}})

    def test_user_config_precedence_and_store_alias_are_resolved_against_explicit_home(self) -> None:
        custom = self.home / "custom"
        custom.mkdir()
        alias = self.home / "store-alias"
        alias.symlink_to(custom, target_is_directory=True)
        (self.home / ".context-guard/config.json").write_text(
            json.dumps(
                {
                    "paths": {"handoff_dir": "~/missing"},
                    "runtimes": {"cursor": {"paths": {"handoff_dir": "~/store-alias"}}},
                }
            )
        )
        self.assertEqual(self.run_helper("install"), 0)
        self.assertEqual(json.loads(self.path.read_text())["permissions"]["allow"], [f"Write({custom}/**)"])
        env = {"CONTEXT_GUARD_PATHS_HANDOFF_DIR": str(self.store)}
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_helper("install", env=env), 2)
        self.assertEqual(self.run_helper("uninstall", env=env), 0)
        self.assertEqual(self.run_helper("install", env=env), 0)
        self.assertEqual(json.loads(self.path.read_text())["permissions"]["allow"], [self.grant])

    def test_uninstall_uses_recorded_grant_after_store_removed(self) -> None:
        self.assertEqual(self.run_helper("install"), 0)
        self.store.rmdir()
        self.assertEqual(self.run_helper("uninstall"), 0)
        self.assertFalse(self.path.exists())

    def test_malformed_json_is_reported_without_changing_bytes(self) -> None:
        for text in ('{"permissions":', "[]", "null"):
            self.path.write_text(text)
            self.assertEqual(self.run_helper("install"), 1)
            self.assertEqual(self.path.read_text(), text)

    def test_readonly_native_config_is_reported_and_preserved(self) -> None:
        self.path.write_text('{"other": true}')
        self.path.chmod(0o444)
        self.addCleanup(self.path.chmod, 0o644)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(self.run_helper("install"), 2)
        self.assertIn("Permission denied", errors.getvalue())
        self.assertEqual(self.path.read_text(), '{"other": true}')

    def test_pattern_characters_cannot_broaden_grant(self) -> None:
        custom = self.home / "wild*"
        custom.mkdir()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_helper("install", env={"CONTEXT_GUARD_PATHS_HANDOFF_DIR": str(custom)}), 2)
        self.assertFalse(self.path.exists())

    def test_explicit_home_alias_uses_same_manifest(self) -> None:
        alias = self.home / "home-alias"
        alias.symlink_to(self.home, target_is_directory=True)
        self.assertEqual(self.run_helper("install"), 0)
        self.assertEqual(
            access.main(["uninstall", "--home", str(alias), "--workspace", str(self.workspace)], {}, io.StringIO()), 0
        )
        self.assertFalse(self.path.exists())

    def test_composed_driver_passes_unchanged_shared_exact_grant_assertion(self) -> None:
        self.driver = Driver(self.home, self.workspace, "access-shared-contract")
        InstallationContracts.test_handoff_access(self)

    def test_malformed_uninstall_preserves_native_bytes_and_ownership_for_retry(self) -> None:
        for malformed in (
            {"permissions": None},
            {"permissions": []},
            {"permissions": {"allow": "broken"}},
            {"permissions": {"allow": [1]}},
            {"permissions": {"deny": {}}},
            {"permissions": {"deny": [None]}},
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
                self.path.write_text(json.dumps({"permissions": {"allow": [self.grant]}}))
                self.assertEqual(self.run_helper("uninstall"), 0)
                self.assertFalse(any(p.exists() for p in manifests))

    def test_two_explicit_homes_keep_independent_project_grants_and_manifests(self) -> None:
        other = self.home / "other-home"
        other.mkdir()
        store = other / ".context-guard/handoffs"
        store.mkdir(parents=True)
        homes = (self.home, other)
        for order in (homes, tuple(reversed(homes))):
            for home in homes:
                self.assertEqual(
                    access.main(
                        ["install", "--home", str(home), "--workspace", str(self.workspace)], {}, io.StringIO()
                    ),
                    0,
                )
                self.assertEqual(
                    len(list((home / ".context-guard/state").glob("cursor-workspace-access-*/install-manifest.json"))),
                    1,
                )
            self.assertEqual(
                set(json.loads(self.path.read_text())["permissions"]["allow"]), {self.grant, f"Write({store}/**)"}
            )
            for home in order:
                self.assertEqual(
                    access.main(
                        ["uninstall", "--home", str(home), "--workspace", str(self.workspace)], {}, io.StringIO()
                    ),
                    0,
                )
            if self.path.exists():
                self.assertEqual(json.loads(self.path.read_text()).get("permissions", {}).get("allow", []), [])
                self.path.unlink()
