"""Configuration precedence: defaults, adapter, user, runtime section, environment."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from core import config
from core.config import ConfigLoader, ConfigSources

ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def sources(self, defaults, adapter=None, runtime="codex"):
        return ConfigSources(runtime, defaults, adapter, self.root / "user.json")

    def test_precedence_defaults_adapter_user_runtime_section_environment(self):
        defaults = self.write("defaults.json", {"policy": {"a": 1, "b": 1, "c": 1, "d": 1, "e": 1}})
        adapter = self.write("adapter.json", {"policy": {"b": 2, "c": 2, "d": 2, "e": 2}, "tiers": {"cheap": "x"}})
        self.write("user.json", {"policy": {"c": 3, "d": 3, "e": 3},
                                 "runtimes": {"codex": {"policy": {"d": 4, "e": 4}},
                                              "cursor": {"policy": {"d": 40}}}})
        loaded = config.load_config(self.sources(defaults, adapter), {"CONTEXT_GUARD_POLICY_E": "5"})
        self.assertEqual(loaded["policy"], {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5})
        self.assertEqual(loaded["tiers"], {"cheap": "x"})
        self.assertNotIn("runtimes", loaded)
        other = config.load_config(self.sources(defaults, adapter, "cursor"), {})
        self.assertEqual(other["policy"]["d"], 40)
        self.assertEqual(config.load_config(self.sources(defaults, None, "claude"), {})["policy"]["b"], 1)

    def test_hook_home_is_the_override_then_the_installed_kit_home_then_the_user_home(self):
        kit = self.root / "sandbox" / ".context-guard" / "kit"
        override = {"CONTEXT_GUARD_HOME": str(self.root / "elsewhere")}
        self.assertEqual(config.resolve_home(kit, override), self.root / "elsewhere")
        self.assertEqual(config.resolve_home(kit, {}), self.root / "sandbox")
        self.assertEqual(config.resolve_home(kit, {"CONTEXT_GUARD_HOME": ""}), self.root / "sandbox")
        for checkout in (ROOT, self.root / "plugins" / "context-guard", self.root / "kit"):
            with self.subTest(checkout=checkout):
                self.assertEqual(config.resolve_home(checkout, {}), Path.home())
        self.assertEqual(config.load_config(config.sources_for("codex", self.root, None), override)["paths"],
                         config.load_config(config.sources_for("codex", self.root, None), {})["paths"])

    def test_only_an_installed_kit_with_another_home_override_is_foreign(self):
        sandbox = self.root / "sandbox"
        kit = sandbox / ".context-guard" / "kit"
        kit.mkdir(parents=True)
        self.assertTrue(config.foreign_home(kit, {"CONTEXT_GUARD_HOME": str(self.root / "elsewhere")}))
        self.assertFalse(config.foreign_home(kit, {"CONTEXT_GUARD_HOME": str(sandbox / ".." / "sandbox")}))
        self.assertFalse(config.foreign_home(kit, {}))
        self.assertFalse(config.foreign_home(kit, {"CONTEXT_GUARD_HOME": ""}))
        for checkout in (ROOT, self.root / "plugins" / "context-guard"):
            with self.subTest(checkout=checkout):
                self.assertFalse(config.foreign_home(checkout, {"CONTEXT_GUARD_HOME": str(self.root / "elsewhere")}))

    def test_shipped_defaults_load_with_documented_sections(self):
        loaded = config.load_config(config.sources_for("cursor", self.root, None), {})
        self.assertEqual(loaded["handoff_list"]["limit"], 5)
        self.assertEqual(loaded["subagent_budget"]["tokens"], 80000)
        self.assertFalse(loaded["runner"]["enabled"])
        self.assertFalse(loaded["classifier"]["enabled"])
        self.assertEqual(config.handoff_dir(loaded, self.root), self.root / ".context-guard/handoffs")
        self.assertEqual(config.state_dir(loaded, self.root, "cursor"), self.root / ".context-guard/state/cursor")
        self.assertEqual(config.kit_root(self.root) / "config.json",
                         config.sources_for("cursor", self.root, None).user)

    def test_invalid_known_user_values_are_ignored_and_unknown_keys_kept(self):
        schema = {"policy": {"count": 10, "ratio": 1.5, "name": "valid", "items": ["a"],
                             "nested": {"flag": False, "keep": 3}}}
        defaults = self.write("defaults.json", schema)
        self.write("user.json", {
            "policy": {"count": True, "ratio": float("nan"), "name": "bad\x00name", "items": [False],
                       "nested": {"flag": "yes", "keep": 7}, "unknown": {"value": None}},
            "metadata": ["kept"], "runtimes": {"codex": {"policy": {"count": "11"}}}})
        loaded = config.load_config(self.sources(defaults), {})
        expected = {"policy": dict(schema["policy"], nested={"flag": False, "keep": 7}, unknown={"value": None}),
                    "metadata": ["kept"]}
        self.assertEqual(loaded, expected)

    def test_new_entries_in_a_section_of_objects_must_be_objects(self):
        defaults = self.write("defaults.json", {"tiers": {"cheap": {"call": "a"}, "deep": {"call": "b"}},
                                                "paths": {"dir": "x"}})
        self.write("user.json", {"tiers": {"huge": "opus", "fast": {"call": "c"}}, "paths": {"extra": 1}})
        loaded = config.load_config(self.sources(defaults), {})
        self.assertEqual(set(loaded["tiers"]), {"cheap", "deep", "fast"})
        self.assertEqual(loaded["paths"], {"dir": "x", "extra": 1})

    def test_environment_overrides_validate_against_shipped_types(self):
        defaults = self.write("defaults.json", {"policy": {"flag": True, "count": 10, "ratio": 1.5,
                                                           "name": "valid", "items": ["a"]}})
        bad = {"CONTEXT_GUARD_POLICY_FLAG": "maybe", "CONTEXT_GUARD_POLICY_COUNT": "true",
               "CONTEXT_GUARD_POLICY_RATIO": "nan", "CONTEXT_GUARD_POLICY_NAME": "\udcff",
               "CONTEXT_GUARD_POLICY_ITEMS": "a,\udcff"}
        self.assertEqual(config.load_config(self.sources(defaults), bad)["policy"],
                         {"flag": True, "count": 10, "ratio": 1.5, "name": "valid", "items": ["a"]})
        good = {"CONTEXT_GUARD_POLICY_FLAG": "false", "CONTEXT_GUARD_POLICY_COUNT": "13",
                "CONTEXT_GUARD_POLICY_RATIO": "2.5", "CONTEXT_GUARD_POLICY_NAME": "renamed",
                "CONTEXT_GUARD_POLICY_ITEMS": "a,b"}
        self.assertEqual(config.load_config(self.sources(defaults), good)["policy"],
                         {"flag": False, "count": 13, "ratio": 2.5, "name": "renamed", "items": ["a", "b"]})

    def test_loader_caches_defensive_copies_and_refreshes_explicitly(self):
        defaults = self.write("defaults.json", {"policy": {"items": [1], "value": 1}})
        environ = {}
        loader = ConfigLoader(self.sources(defaults), environ)
        returned = loader.load()
        returned["policy"]["items"].append(9)
        self.assertEqual(loader.load()["policy"], {"items": [1], "value": 1})
        environ["CONTEXT_GUARD_POLICY_VALUE"] = "6"
        self.assertEqual(loader.load()["policy"]["value"], 1)
        self.assertEqual(loader.load(refresh=True)["policy"]["value"], 6)

    def test_updates_and_removals_write_the_user_file_and_refresh(self):
        defaults = self.write("defaults.json", {"policy": {"value": 1}})
        user = self.write("user.json", {"policy": {"value": 2, "keep": 3}, "obsolete": {"key": 4},
                                        "runtimes": {"codex": {"policy": {"keep": 5}}}})
        loader = ConfigLoader(self.sources(defaults), {})
        self.assertEqual(loader.update_user_config({"policy": {"added": [5]}}, ("policy.value", "obsolete.key")), user)
        self.assertEqual(json.loads(user.read_text()), {"policy": {"keep": 3, "added": [5]},
                                                        "runtimes": {"codex": {"policy": {"keep": 5}}}})
        self.assertEqual(loader.load()["policy"], {"value": 1, "keep": 5, "added": [5]})
        self.assertFalse(list(self.root.glob("user.json.*.tmp")))

    def test_malformed_user_files_fail_open_for_load_and_refuse_updates(self):
        defaults = self.write("defaults.json", {"policy": {"value": 1}})
        user = self.root / "user.json"
        loader = ConfigLoader(self.sources(defaults), {})
        for contents, error in ((b"{", ValueError), (b"[]", TypeError), (b"\xff", UnicodeDecodeError)):
            with self.subTest(contents=contents):
                user.write_bytes(contents)
                self.assertEqual(loader.load(refresh=True), {"policy": {"value": 1}})
                with self.assertRaises(error):
                    loader.update_user_config({"policy": {"value": 2}})
                self.assertEqual(user.read_bytes(), contents)

    def test_concurrent_process_updates_preserve_distinct_values(self):
        user = self.write("user.json", {"policy": {"existing": "keep"}})
        code = ("import sys; from pathlib import Path; from core.config import ConfigLoader, ConfigSources; "
                "path = Path(sys.argv[1]); "
                "ConfigLoader(ConfigSources('codex', path, None, path), {})"
                ".update_user_config({'policy': {sys.argv[2]: sys.argv[2]}})")
        keys = [f"worker-{index}" for index in range(8)]
        children = [subprocess.Popen([sys.executable, "-c", code, str(user), key], cwd=ROOT,
                                     stderr=subprocess.PIPE, text=True) for key in keys]
        for child in children:
            _, error = child.communicate(timeout=15)
            self.assertEqual(child.returncode, 0, error)
        self.assertEqual(json.loads(user.read_text()), {"policy": {"existing": "keep", **{key: key for key in keys}}})


if __name__ == "__main__":
    unittest.main()
