"""Claude configuration profiles keep settings, ownership and offers independent."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.claude import access, hooks, mapping
from core import install
from tests import harness


class ProfileTests(unittest.TestCase):
    """Two Claude profiles share a temporary home and context-guard store."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.config = hooks.load_config(self.home, {})
        self.profiles = [self.home / "profile-a", self.home / "profile-b"]
        self.envs = [{"CLAUDE_CONFIG_DIR": str(root)} for root in self.profiles]

    def test_resolution_defaults_expansion_normalization_and_rejection(self) -> None:
        for environ in ({}, {"CLAUDE_CONFIG_DIR": ""}):
            self.assertEqual(mapping.claude_home(self.home, environ), self.home / ".claude")
            self.assertEqual(mapping.install_identity(self.home, "claude", environ), "claude")
        self.assertEqual(mapping.claude_home(self.home, {"CLAUDE_CONFIG_DIR": "~/profile-a"}), self.profiles[0])
        with patch.dict("os.environ", self.envs[0], clear=True):
            self.assertEqual(mapping.claude_home(self.home), self.profiles[0])
        with self.assertRaisesRegex(ValueError, "CLAUDE_CONFIG_DIR"):
            mapping.install_spec(self.home, self.home / "kit", Path("/python"), self.config, {"CLAUDE_CONFIG_DIR": "x"})
        names = [mapping.install_identity(self.home, "claude", env) for env in self.envs]
        self.assertNotEqual(*names)
        for name in names:
            self.assertRegex(name, r"^claude-[0-9a-f]{32}$")

    def test_script_install_files_settings_and_signatures_follow_each_profile(self) -> None:
        for root, env in zip(self.profiles, self.envs, strict=True):
            spec = mapping.install_spec(self.home, self.home / "kit", Path("/python"), self.config, env)
            self.assertEqual(spec.runtime, "claude")
            self.assertTrue(all(path.is_relative_to(root) for path in spec.files))
            self.assertTrue(all(path.is_relative_to(root / "agents") for path in spec.kit_signatures))
            self.assertEqual(spec.json_merges[0].path, root / "settings.json")
            identity = mapping.install_identity(self.home, mapping.RUNTIME, env)
            install.apply(install.plan(spec, {}), install.manifest_path(self.home, identity), self.home / "backups")
            self.assertTrue((root / "rules" / "context-guard.md").is_file())
            self.assertIn("hooks", json.loads((root / "settings.json").read_text()))
        self.assertFalse((self.home / ".claude").exists())

    def test_profile_offers_ignore_other_profile_markers_and_manifests(self) -> None:
        self.assertIn(str(self.profiles[0] / "settings.json"), hooks.access_offer(self.config, self.home, self.envs[0]))
        self.assertEqual(hooks.access_offer(self.config, self.home, self.envs[0]), "")
        installed = mapping.install_identity(self.home, mapping.RUNTIME, self.envs[0])
        manifest = install.manifest_path(self.home, installed)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{}")
        self.assertIn("access.py install", hooks.access_offer(self.config, self.home, self.envs[1]))
        self.assertEqual(hooks.access_offer(self.config, self.home, self.envs[1]), "")
        self.assertFalse(install.offer_marker(self.home, mapping.PLUGIN_ACCESS).exists())

    def test_access_installs_and_uninstalls_independently_preserving_user_settings(self) -> None:
        originals = ['{\n "model": "opus"\n}', '{"permissions": {"allow": ["Bash(ls)"]}}']
        for root, env, original in zip(self.profiles, self.envs, originals, strict=True):
            root.mkdir()
            (root / "settings.json").write_text(original)
            args = ["install", "--home", str(self.home)]
            self.assertEqual(access.main(args, env, io.StringIO()), access.EXIT_OK)
            self.assertFalse(hooks.lacks_access(self.config, self.home, env))
            identity = mapping.install_identity(self.home, mapping.PLUGIN_ACCESS, env)
            self.assertTrue(install.manifest_path(self.home, identity).is_file())
            self.assertTrue(install.offer_marker(self.home, identity).is_file())
        self.assertEqual(
            access.main(["uninstall", "--home", str(self.home)], self.envs[0], io.StringIO()), access.EXIT_OK
        )
        self.assertEqual((self.profiles[0] / "settings.json").read_text(), originals[0])
        self.assertFalse(hooks.lacks_access(self.config, self.home, self.envs[1]))
        user = json.loads((self.profiles[1] / "settings.json").read_text())
        user["theme"] = "dark"
        (self.profiles[1] / "settings.json").write_text(json.dumps(user))
        self.assertEqual(
            access.main(["uninstall", "--home", str(self.home)], self.envs[1], io.StringIO()), access.EXIT_OK
        )
        self.assertEqual(
            json.loads((self.profiles[1] / "settings.json").read_text()), {**json.loads(originals[1]), "theme": "dark"}
        )

    def test_permission_detection_uses_selected_settings(self) -> None:
        self.profiles[0].mkdir()
        rule = mapping.access_rule(self.home / ".context-guard" / "handoffs")
        (self.profiles[0] / "settings.json").write_text(json.dumps({"permissions": {"allow": [rule]}}))
        self.assertFalse(hooks.lacks_access(self.config, self.home, self.envs[0]))
        self.assertTrue(hooks.lacks_access(self.config, self.home, self.envs[1]))

    def test_transcript_usage_follows_payload_outside_selected_profile(self) -> None:
        path = self.home / "relocated-transcripts" / "session.jsonl"
        harness.append_transcript(path, harness.assistant_usage(950_000, "claude-sonnet-5-5", "2099-01-01T00:00:00Z"))
        payload = harness.payload("claude", "UserPromptSubmit-root", self.home, self.home)
        payload.update(session_id="relocated", transcript_path=str(path), cwd=str(self.home))
        response = hooks.run("UserPromptSubmit", payload, self.home, self.envs[0])
        self.assertTrue(response)
        self.assertFalse((self.home / ".claude").exists())

    def test_invalid_profile_fails_open_in_hook(self) -> None:
        payload = {"session_id": "s1", "cwd": str(self.home)}
        self.assertEqual(hooks.run("SessionStart", payload, self.home, {"CLAUDE_CONFIG_DIR": "relative"}), {})
