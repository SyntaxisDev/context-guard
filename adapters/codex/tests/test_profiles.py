"""Native profile installation and plugin state remain independent."""

import io
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from adapters.codex import access, agents, hooks, mapping
from core import install


class ProfileTests(unittest.TestCase):
    """Use temporary native roots for every installation and offer."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.profile = self.home / "alternate"
        self.environ = {"CODEX_HOME": str(self.profile)}
        self.config = hooks.load_config(self.home, {})

    def test_install_targets_every_file_and_registration_at_native_root(self) -> None:
        spec = mapping.install_spec(self.home, self.home / "kit", self.home / "python", self.config, self.environ)
        self.assertEqual(spec.runtime, mapping.RUNTIME)
        paths = [
            *spec.files,
            *spec.kit_signatures,
            *(item.path for item in spec.json_merges),
            *(item.path for item in spec.text_edits),
        ]
        self.assertTrue(all(path.is_relative_to(self.profile) for path in paths))
        document = tomllib.loads(spec.text_edits[0].apply(""))
        for name, agent in document["agents"].items():
            self.assertEqual(agent["config_file"], str(self.profile / "agents" / f"{name}.toml"))
        with mock.patch.dict("os.environ", self.environ):
            self.assertEqual(
                mapping.install_spec(self.home, self.home / "kit", self.home / "python", self.config).files, spec.files
            )

    def test_identity_keeps_default_and_relative_native_roots_unchanged(self) -> None:
        for environment in ({}, {"CODEX_HOME": "relative"}, {"CODEX_HOME": str(self.home / ".codex")}):
            self.assertEqual(mapping.install_identity(self.home, mapping.RUNTIME, environment), mapping.RUNTIME)
        self.assertRegex(mapping.install_identity(self.home, mapping.RUNTIME, self.environ), r"^codex-[0-9a-f]{32}$")

    def test_plugin_helpers_uninstall_only_the_selected_profile(self) -> None:
        for helper in (agents, access):
            with self.subTest(helper=helper.__name__):
                argv = ["install", "--home", str(self.home)]
                self.assertEqual(helper.main(argv, {}, io.StringIO()), 0)
                self.assertEqual(helper.main(argv, self.environ, io.StringIO()), 0)
                default = helper.plan("install", self.home, {}, force=False)
                alternate = helper.plan("install", self.home, self.environ, force=False)
                self.assertFalse(default.changes)
                self.assertFalse(alternate.changes)
                before = {path: path.read_bytes() for path in (self.home / ".codex").rglob("*") if path.is_file()}
                self.assertEqual(helper.main(["uninstall", "--home", str(self.home)], self.environ, io.StringIO()), 0)
                self.assertTrue(all(path.read_bytes() == data for path, data in before.items()))
                self.assertTrue(install.manifest_path(self.home, helper.MANIFEST_RUNTIME).exists())
                identity = mapping.install_identity(self.home, helper.MANIFEST_RUNTIME, self.environ)
                self.assertFalse(install.manifest_path(self.home, identity).exists())

    def test_default_profile_install_and_offer_do_not_suppress_alternate_offers(self) -> None:
        for name in (mapping.RUNTIME, mapping.PLUGIN_AGENTS, mapping.PLUGIN_ACCESS):
            path = install.manifest_path(self.home, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}")
        install.record_offer(self.home, mapping.PLUGIN_AGENTS)
        install.record_offer(self.home, mapping.PLUGIN_ACCESS)
        self.assertTrue(hooks.agents_offer(self.home, self.environ))
        self.assertTrue(hooks.access_offer(self.config, self.home, self.environ))
        self.assertFalse(hooks.agents_offer(self.home, self.environ))
        self.assertFalse(hooks.access_offer(self.config, self.home, self.environ))
