"""Independent runtime profiles share a kit without sharing installation ownership."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import cli
from core import kit
from tests.test_cli import TOOLCHAIN
from tests.test_install import snapshot

PROFILE_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "genie": "GENIE_HOME"}


class ProfileLifecycleTests(unittest.TestCase):
    """Install, inspect and remove two profiles without disturbing their files or shared kit."""

    def command(self, home: Path, runtime: str, root: Path | None, *args: str) -> tuple[int, str]:
        """Run the real adapter CLI with a temporary profile and an inexpensive toolchain.

        Args:
            home: Temporary user home.
            runtime: Adapter being tested.
            root: Alternate profile, or None for the default.
            *args: CLI command and extra options.

        Returns:
            The exit code and captured output.
        """
        environment = dict.fromkeys(PROFILE_ENV.values(), "")
        if root is not None:
            environment[PROFILE_ENV[runtime]] = str(root)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environment), redirect_stderr(err):
            code = cli.main([*args, "--runtime", runtime, "--home", str(home)], toolchain=TOOLCHAIN, out=out)
        self.assertEqual(err.getvalue(), "")
        return code, out.getvalue()

    def lifecycle(self, home: Path, runtime: str, *, custom_first: bool) -> None:
        """Check two profiles through both removal orders.

        Args:
            home: Temporary user home.
            runtime: Adapter being tested.
            custom_first: Whether to uninstall the alternate profile before the default.
        """
        default, custom = home / f".{runtime}", home / f".{runtime}_personal"
        for root in (default, custom):
            root.mkdir(parents=True)
            filename = "settings.json" if runtime == "claude" else "hooks.json"
            (root / filename).write_text(
                '{ "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo user"}]}]}, "user": true }\n'
            )
            if runtime != "claude":
                (root / "config.toml").write_text('model = "my-model"\n')
        originals = {root: snapshot(root) for root in (default, custom)}
        self.assertEqual(self.command(home, runtime, None, "install")[0], cli.EXIT_OK)
        installed_default = snapshot(default)
        self.assertEqual(self.command(home, runtime, custom, "install")[0], cli.EXIT_OK)
        self.assertEqual(snapshot(default), installed_default)
        installed_custom = snapshot(custom)
        alias = home / "profile-alias"
        alias.symlink_to(custom, target_is_directory=True)
        self.assertEqual(self.command(home, runtime, alias, "install")[0], cli.EXIT_OK)
        self.assertEqual(snapshot(custom), installed_custom)
        alias.unlink()
        for selected in (None, custom):
            self.assertEqual(self.command(home, runtime, selected, "doctor")[0], cli.EXIT_OK)
            self.assertIn("installed", self.command(home, runtime, selected, "status")[1])
        before = snapshot(home)
        self.assertEqual(self.command(home, runtime, custom, "uninstall", "--dry-run")[0], cli.EXIT_OK)
        self.assertEqual(snapshot(home), before)
        first, last = (custom, None) if custom_first else (None, custom)
        self.assertEqual(self.command(home, runtime, first, "uninstall")[0], cli.EXIT_OK)
        self.assertTrue(kit.venv_python(kit.kit_dir(home)).exists())
        self.assertEqual(self.command(home, runtime, last, "doctor")[0], cli.EXIT_OK)
        self.assertEqual(self.command(home, runtime, last, "uninstall")[0], cli.EXIT_OK)
        self.assertFalse(kit.kit_dir(home).exists())
        for root, original in originals.items():
            self.assertEqual(snapshot(root), original)

    def test_two_profiles_restore_originals_and_keep_kit_until_last_uninstall(self) -> None:
        for runtime in PROFILE_ENV:
            for custom_first in (True, False):
                with self.subTest(runtime=runtime, custom_first=custom_first), tempfile.TemporaryDirectory() as temp:
                    self.lifecycle(Path(temp).resolve(), runtime, custom_first=custom_first)
