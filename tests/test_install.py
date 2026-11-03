"""Install engine contracts: planning, conflicts, backups, reversal, doctor and the kit copy."""

import json
import os
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from core import install, kit
from core.install_spec import InstallSpec, JsonMerge, TextEdit

HOOK_KEY = "context-guard"


def hook_command(kit_root: Path, python: Path, runtime: str) -> str:
    return f"{python} {kit_root}/adapters/{runtime}/hooks.py session"


def merge_hooks(command: str) -> tuple[Callable[[dict], dict], Callable[[dict], dict]]:
    def merge(data: dict) -> dict:
        data.setdefault("hooks", {})[HOOK_KEY] = command
        return data

    def unmerge(data: dict) -> dict:
        hooks = data.get("hooks", {})
        hooks.pop(HOOK_KEY, None)
        if not hooks:
            data.pop("hooks", None)
        return data

    return merge, unmerge


def append_marker(text: str) -> str:
    return text if "context-guard = on" in text else text + "context-guard = on\n"


def remove_marker(text: str) -> str:
    return text.replace("context-guard = on\n", "")


def fake_spec(home: Path, kit_root: Path, python: Path, _config: dict, runtime: str = "fake") -> InstallSpec:
    """A runtime mapping stand-in: owned files, a hook merge and a text edit.

    Returns:
        The spec, rooted at `<home>/.<runtime>-home`.
    """
    base = home / f".{runtime}-home"
    merge, unmerge = merge_hooks(hook_command(kit_root, python, runtime))
    return InstallSpec(
        runtime,
        files={base / "agents" / "scout.md": "scout\n", base / "rules" / "policy.md": "policy\n"},
        json_merges=[JsonMerge(base / "settings.json", merge, unmerge)],
        text_edits=[TextEdit(base / "config.toml", append_marker, remove_marker)],
    )


def snapshot(root: Path, ignored: tuple[str, ...] = ()) -> dict:
    found = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if any(relative == item or relative.startswith(item + "/") for item in ignored):
            continue
        if path.is_symlink():
            found[relative] = f"<link {path.readlink()}>"
        else:
            found[relative] = path.read_bytes() if path.is_file() else "<dir>"
    return found


class EngineCase(unittest.TestCase):
    """A temporary home with the fake spec and its manifest, installed through the engine."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.kit = kit.kit_dir(self.home)
        self.python = kit.venv_python(self.kit)
        self.base = self.home / ".fake-home"
        self.manifest_file = install.manifest_path(self.home, "fake")
        self.backups = self.home / ".context-guard" / "backups" / "test"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def spec(self, **changes: object) -> InstallSpec:
        spec = fake_spec(self.home, self.kit, self.python, {})
        return InstallSpec(**{**spec.__dict__, **changes})

    def manifest(self) -> dict:
        return install.read_manifest(self.manifest_file)

    def install(self, spec: InstallSpec | None = None, *, force: bool = False) -> install.Plan:
        plan = install.plan(spec or self.spec(), self.manifest(), force=force)
        install.apply(plan, self.manifest_file, self.backups)
        return plan

    def uninstall(self, spec: InstallSpec | None = None, *, force: bool = False) -> install.Plan:
        plan = install.plan_uninstall(spec or self.spec(), self.manifest(), force=force)
        install.apply(plan, self.manifest_file, self.backups)
        return plan

    def write(self, relative: str, text: str) -> Path:
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


class PlanTests(EngineCase):
    """Planning is side-effect free, idempotent, recorded in the manifest and conflict-aware."""

    def test_plan_writes_nothing(self) -> None:
        self.write("settings.json", '{"theme": "dark"}\n')
        before = snapshot(self.home)
        plan = install.plan(self.spec(), {})
        self.assertTrue(plan.changes)
        self.assertEqual(snapshot(self.home), before)

    def test_reinstall_is_idempotent(self) -> None:
        self.install()
        before = snapshot(self.home)
        plan = self.install()
        self.assertEqual((plan.changes, plan.conflicts), ([], []))
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(install.describe(plan, dry_run=False), ["[fake] up to date"])

    def test_an_edit_notice_is_reported_as_a_note_without_a_conflict(self) -> None:
        config = self.base / "config.toml"
        edit = TextEdit(config, append_marker, remove_marker, lambda text: "finish it" if "on" in text else "")
        self.install()
        plan = self.install(self.spec(text_edits=[edit]))
        self.assertEqual((plan.changes, plan.conflicts), ([], []))
        self.assertEqual(
            install.describe(plan, dry_run=False), ["[fake] up to date", f"[fake] note: {config}: finish it"]
        )

    def test_manifest_records_hashes_merges_and_edits(self) -> None:
        self.install()
        manifest = self.manifest()
        self.assertEqual(manifest["files"][str(self.base / "agents" / "scout.md")], install.digest("scout\n"))
        self.assertTrue(manifest["merges"][str(self.base / "settings.json")]["created"])
        self.assertIn(str(self.base / "config.toml"), manifest["edits"])

    def test_user_edited_owned_file_conflicts_until_forced(self) -> None:
        self.install()
        agent = self.write("agents/scout.md", "my scout\n")
        plan = self.install()
        self.assertEqual([c.path for c in plan.conflicts], [agent])
        self.assertIn("-my scout", plan.conflicts[0].message)
        self.assertEqual(agent.read_text(), "my scout\n")
        self.install(force=True)
        self.assertEqual(agent.read_text(), "scout\n")
        records = [json.loads(line) for line in (self.backups / "paths.jsonl").read_text().splitlines()]
        saved = [self.backups / r["backup"] for r in records if r["path"] == str(agent)]
        self.assertEqual([path.read_text() for path in saved], ["my scout\n"])

    def test_unmanaged_existing_file_is_a_conflict(self) -> None:
        agent = self.write("agents/scout.md", "hand written\n")
        plan = self.install()
        self.assertEqual([c.path for c in plan.conflicts], [agent])
        self.assertEqual(agent.read_text(), "hand written\n")

    def test_rewrite_replaces_whole_files_and_keeps_links_and_modes(self) -> None:
        real = self.home / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text('{"theme": "dark"}\n')
        settings = self.base / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.symlink_to(real)
        self.install()
        self.assertTrue(settings.is_symlink())
        self.assertIn("hooks", json.loads(real.read_text()))
        agent = self.base / "agents" / "scout.md"
        agent.chmod(0o640)
        with agent.open() as reader:
            self.install(self.spec(files={**self.spec().files, agent: "scout v2\n"}))
            self.assertEqual(reader.read(), "scout\n")
        self.assertEqual(agent.read_text(), "scout v2\n")
        self.assertEqual(agent.stat().st_mode & 0o777, 0o640)
        self.assertFalse(list(self.home.rglob("*.tmp")))

    def test_removed_spec_file_is_retired_only_when_unmodified(self) -> None:
        self.install()
        files = dict(self.spec().files)
        rule = self.base / "rules" / "policy.md"
        del files[rule]
        self.install(self.spec(files=files))
        self.assertFalse(rule.exists())
        self.assertNotIn(str(rule), self.manifest()["files"])


class MergeAndEditTests(EngineCase):
    """JSON merges and text edits keep user content and reverse cleanly."""

    def test_json_merge_preserves_unrelated_keys_and_unmerge_restores(self) -> None:
        original = {"theme": "dark", "hooks": {"other": "keep"}}
        settings = self.write("settings.json", json.dumps(original))
        self.install()
        merged = json.loads(settings.read_text())
        self.assertEqual(merged["theme"], "dark")
        self.assertEqual(merged["hooks"]["other"], "keep")
        self.assertIn(f"{self.kit}/adapters/fake/hooks.py", merged["hooks"][HOOK_KEY])
        self.uninstall()
        self.assertEqual(json.loads(settings.read_text()), original)

    def test_uninstall_restores_original_bytes_unless_the_user_changed_settings_since(self) -> None:
        text = '{"theme":   "dark",\n "hooks": {"other": "keep"}}'
        settings = self.write("settings.json", text)
        self.install()
        self.install()
        self.uninstall()
        self.assertEqual(settings.read_text(), text)
        self.install()
        edited = json.loads(settings.read_text()) | {"font": 14}
        settings.write_text(json.dumps(edited))
        self.uninstall()
        self.assertEqual(json.loads(settings.read_text()), {"theme": "dark", "hooks": {"other": "keep"}, "font": 14})

    def test_install_leaves_a_file_already_holding_our_merge_untouched(self) -> None:
        self.install()
        settings = self.base / "settings.json"
        reformatted = json.dumps(json.loads(settings.read_text()), indent=4)
        settings.write_text(reformatted)
        plan = self.install()
        self.assertNotIn(settings, [change.path for change in plan.changes])
        self.assertEqual(settings.read_text(), reformatted)

    def test_merge_into_invalid_json_is_a_conflict(self) -> None:
        settings = self.write("settings.json", "{not json")
        plan = self.install()
        self.assertEqual([c.path for c in plan.conflicts], [settings])
        self.assertEqual(settings.read_text(), "{not json")

    def test_created_merge_and_edit_targets_are_removed_on_uninstall(self) -> None:
        self.install()
        self.assertTrue((self.base / "settings.json").exists())
        self.uninstall()
        self.assertFalse((self.base / "settings.json").exists())
        self.assertFalse((self.base / "config.toml").exists())

    def test_existing_merge_target_left_empty_is_removed_with_a_backup(self) -> None:
        text = json.dumps({"hooks": {HOOK_KEY: "an older install"}})
        settings = self.write("settings.json", text)
        self.install()
        self.uninstall()
        self.assertFalse(settings.exists())
        records = [json.loads(line) for line in (self.backups / "paths.jsonl").read_text().splitlines()]
        self.assertIn(str(settings), [record["path"] for record in records])

    def test_existing_empty_merge_target_is_restored_not_removed(self) -> None:
        settings = self.write("settings.json", "{}\n")
        self.install()
        self.uninstall()
        self.assertEqual(settings.read_text(), "{}\n")

    def test_text_edit_revert_keeps_user_text(self) -> None:
        config = self.write("config.toml", "model = 'x'\n")
        self.install()
        self.assertEqual(config.read_text(), "model = 'x'\ncontext-guard = on\n")
        self.uninstall()
        self.assertEqual(config.read_text(), "model = 'x'\n")


class UninstallTests(EngineCase):
    """Uninstall restores the home and keeps user-edited files in a residual manifest."""

    def test_uninstall_restores_home(self) -> None:
        self.write("settings.json", json.dumps({"theme": "dark"}, indent=2) + "\n")
        self.write("config.toml", "model = 'x'\n")
        before = snapshot(self.home)
        self.install()
        self.uninstall()
        self.assertEqual(snapshot(self.home, (".context-guard",)), before)
        self.assertEqual([p.name for p in (self.home / ".context-guard").iterdir()], ["backups"])

    def test_edited_file_survives_uninstall_in_residual_manifest(self) -> None:
        self.install()
        agent = self.write("agents/scout.md", "mine\n")
        plan = self.uninstall()
        self.assertEqual([c.path for c in plan.conflicts], [agent])
        self.assertEqual(agent.read_text(), "mine\n")
        self.assertEqual(list(self.manifest()["files"]), [str(agent)])
        self.uninstall(force=True)
        self.assertFalse(agent.exists())
        self.assertFalse(self.manifest_file.exists())


class DoctorTests(EngineCase):
    """Doctor reports file drift, missing merges and hooks outside the kit."""

    def problems(self, spec: InstallSpec | None = None) -> list[str]:
        return install.doctor(spec or self.spec(), self.manifest(), self.kit, self.python)

    def test_clean_install_has_no_problems(self) -> None:
        self.install()
        self.assertEqual(self.problems(), [])

    def test_detects_file_drift_and_missing_merge(self) -> None:
        self.install()
        self.write("rules/policy.md", "changed\n")
        self.write("settings.json", "{}\n")
        problems = "\n".join(self.problems())
        self.assertIn("policy.md differs", problems)
        self.assertIn("settings.json lacks", problems)

    def test_detects_hook_outside_kit(self) -> None:
        self.install()
        settings = self.base / "settings.json"
        data = json.loads(settings.read_text())
        data["hooks"]["stale"] = "python3 /elsewhere/adapters/fake/hooks.py session"
        settings.write_text(json.dumps(data))
        self.assertTrue(any("outside" in problem for problem in self.problems()))


class KitTests(unittest.TestCase):
    """The kit copy skips caches and tests, retires stale files and protects edits."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source, self.home = root / "source", root / "home"
        self.kit = kit.kit_dir(self.home)
        for relative in (
            "core/a.py",
            "core/b.py",
            "content/x.md",
            "adapters/fake/hooks.py",
            "adapters/fake/tests/test_x.py",
            "core/__pycache__/a.cpython-314.pyc",
            "cli.py",
        ):
            (self.source / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.source / relative).write_text(relative)
        self.manifest_file = install.manifest_path(self.home, kit.KIT_RUNTIME)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def install(self) -> install.Plan:
        plan = install.plan(kit.kit_spec(self.kit, self.source), install.read_manifest(self.manifest_file))
        install.apply(plan, self.manifest_file, self.home / "backups")
        return plan

    def test_kit_copy_skips_caches_and_tests(self) -> None:
        self.install()
        self.assertEqual(
            sorted(p.relative_to(self.kit).as_posix() for p in self.kit.rglob("*") if p.is_file()),
            ["adapters/fake/hooks.py", "cli.py", "content/x.md", "core/a.py", "core/b.py"],
        )

    def test_kit_copy_removes_stale_files(self) -> None:
        self.install()
        (self.source / "core" / "b.py").unlink()
        plan = self.install()
        self.assertEqual([(c.kind, c.path) for c in plan.changes], [(install.REMOVE, self.kit / "core" / "b.py")])
        self.assertFalse((self.kit / "core" / "b.py").exists())

    def test_edited_kit_file_conflicts(self) -> None:
        self.install()
        (self.kit / "core" / "a.py").write_text("patched")
        (self.source / "core" / "a.py").write_text("new")
        plan = self.install()
        self.assertEqual([c.path for c in plan.conflicts], [self.kit / "core" / "a.py"])
        self.assertEqual((self.kit / "core" / "a.py").read_text(), "patched")


class InterpreterTests(unittest.TestCase):
    """Interpreter discovery reports broken, old and missing Pythons and reuses a usable venv."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_broken_interpreter_is_reported(self) -> None:
        broken = self.root / "python"
        broken.write_text("#!/bin/sh\nexit 1\n")
        broken.chmod(0o755)
        self.assertIn("does not run", kit.interpreter_problem(broken, kit.Toolchain()))

    def test_old_interpreter_is_reported(self) -> None:
        old = kit.Toolchain(version=lambda _python: (3, 9))
        self.assertIn("3.9", kit.interpreter_problem(Path("python"), old))

    def test_missing_python_gives_install_hint(self) -> None:
        with (
            mock.patch.dict(os.environ, {"PATH": str(self.root)}),
            self.assertRaisesRegex(kit.ToolchainError, "uv python install"),
        ):
            kit.find_python()

    def test_venv_is_created_only_when_unusable(self) -> None:
        created = []

        def create(base: Path, venv: Path) -> None:
            created.append(base)
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin" / "python").symlink_to(base)

        toolchain = kit.Toolchain(find_python=lambda: Path(sys.executable), create_venv=create)
        kit_root = self.root / "kit"
        base = kit.venv_base(kit_root, toolchain)
        self.assertEqual(kit.create_kit_venv(kit_root, base, toolchain), kit.venv_python(kit_root))
        self.assertIsNone(kit.venv_base(kit_root, toolchain))
        self.assertEqual(created, [Path(sys.executable)])


if __name__ == "__main__":
    unittest.main()
