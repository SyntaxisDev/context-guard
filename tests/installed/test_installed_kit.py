"""The installed kit end to end, from `cli.py install --runtime all --home <tmp>` to uninstall.

Install uses real interpreter discovery, then the exact command strings written to hooks.json /
settings.json run as the runtime would run them, fed recorded payloads, with telemetry (Cursor
state.vscdb, Codex rollouts, Claude transcripts) under the temporary home.
"""

import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from core import state
from tests import harness

CLI = harness.ROOT / "cli.py"
CODEX_WINDOW = 258_400
ROOT_PROMPTS = {
    "cursor": "beforeSubmitPrompt-root",
    "codex": "UserPromptSubmit-root",
    "claude": "UserPromptSubmit-root",
}
USER_FILES = {
    ".cursor/hooks.json": json.dumps(
        {"version": 1, "hooks": {"afterFileEdit": [{"command": "./format.sh"}], "stop": [{"command": "say done"}]}},
        indent=2,
    )
    + "\n",
    ".codex/hooks.json": json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}, indent=2
    )
    + "\n",
    ".codex/config.toml": 'model = "gpt-6.1-sol"\n\n[features]\nweb_search = true\n',
    ".codex/AGENTS.md": "# My global Codex notes\n",
    ".claude/settings.json": json.dumps(
        {
            "model": "sonnet",
            "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]},
        },
        indent=2,
    )
    + "\n",
    ".claude/CLAUDE.md": "# My global Claude notes\n",
}


def cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), *args, "--home", str(home)],
        capture_output=True,
        text=True,
        env=harness.hook_env(home),
        timeout=300,
        check=False,
    )


def seed(home: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text(text)


def kit_command(command: str, home: Path) -> bool:
    return str(home / ".context-guard" / "kit") in command


def registered(home: Path) -> dict[str, dict[str, str]]:
    """The kit hook commands an install registered.

    Returns:
        Per runtime, native event to the exact command string.
    """
    cursor = json.loads((home / ".cursor/hooks.json").read_text())["hooks"]
    found = {
        "cursor": {
            event: entry["command"]
            for event, entries in cursor.items()
            for entry in entries
            if kit_command(entry.get("command", ""), home)
        }
    }
    for runtime, path in (("codex", ".codex/hooks.json"), ("claude", ".claude/settings.json")):
        hooks = json.loads((home / path).read_text())["hooks"]
        found[runtime] = {
            event: hook["command"]
            for event, groups in hooks.items()
            for group in groups
            for hook in group.get("hooks", [])
            if kit_command(hook.get("command", ""), home)
        }
    return found


class Kit:
    """The module-wide install: its home, git project and registered commands."""

    home: Path
    project: Path
    commands: dict[str, dict[str, str]]


def setUpModule() -> None:
    temporary = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(temporary.cleanup)
    Kit.home = Path(temporary.name).resolve()
    Kit.project = Kit.home / "dev" / "project"
    (Kit.project / ".git").mkdir(parents=True)
    seed(Kit.home, USER_FILES)
    result = cli(Kit.home, "install", "--runtime", "all")
    if result.returncode != 0:
        msg = f"install failed ({result.returncode}): {result.stdout}{result.stderr}"
        raise RuntimeError(msg)
    Kit.commands = registered(Kit.home)


class InstalledCase(unittest.TestCase):
    """One runtime's registered commands, fired with recorded payloads in a fresh session."""

    runtime = ""

    def setUp(self) -> None:
        self.home, self.project, self.session = Kit.home, Kit.project, str(uuid.uuid4())

    def fire(self, fixture: str, cwd: Path | None = None, **overrides: object) -> dict:
        data = harness.payload(self.runtime, fixture, self.home, self.project, **{**self.ids(), **overrides})
        command = Kit.commands[self.runtime][data["hook_event_name"]]
        run = harness.run_hook(command, data, self.home, cwd or self.project)
        self.assertEqual((run.returncode, run.stderr), (0, ""), command)
        response = run.response()
        self.assertEqual(harness.response_problems(self.runtime, data["hook_event_name"], response), [])
        return response

    def ids(self) -> dict:
        return {}

    def handoff_file(self) -> Path:
        return harness.seed_handoff(self.home, self.project, f"Installed {self.runtime} task {self.session[:8]}")

    def walked(self) -> list[str]:
        return [
            entry["event"]
            for entry in harness.events(self.home, self.runtime)
            if entry["session"] == self.session and entry["event"] != "prompt"
        ]


class HomeResolutionTests(unittest.TestCase):
    """Installed hooks log under the kit home and stay inert for another CONTEXT_GUARD_HOME."""

    def prompt(self, runtime: str, session: str, env: dict) -> dict:
        ids = {"conversation_id": session} if runtime == "cursor" else {}
        data = harness.payload(
            runtime, ROOT_PROMPTS[runtime], Kit.home, Kit.project, session_id=session, transcript_path=None, **ids
        )
        run = harness.run_hook(Kit.commands[runtime][data["hook_event_name"]], data, Kit.home, Kit.project, env)
        self.assertEqual((run.returncode, run.stderr), (0, ""))
        return run.response()

    def sessions(self, home: Path, runtime: str) -> set[str]:
        return {entry["session"] for entry in harness.events(home, runtime)}

    def test_installed_hooks_use_the_kit_home_also_when_context_guard_home_names_it(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        other = Path(temporary.name) / "real-home"
        for runtime in harness.RUNTIMES:
            with self.subTest(runtime=runtime):
                kit_session, env_session = str(uuid.uuid4()), str(uuid.uuid4())
                self.prompt(runtime, kit_session, {"HOME": str(other)})
                self.prompt(runtime, env_session, {"HOME": str(other), "CONTEXT_GUARD_HOME": str(Kit.home)})
                self.assertLessEqual({kit_session, env_session}, self.sessions(Kit.home, runtime))
        self.assertFalse((other / ".context-guard").exists())

    def test_installed_hooks_stay_inert_when_context_guard_home_names_another_home(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        override = Path(temporary.name) / "override"
        passing = {"cursor": {"continue": True}, "codex": {}, "claude": {}}
        for runtime in harness.RUNTIMES:
            with self.subTest(runtime=runtime):
                session = str(uuid.uuid4())
                response = self.prompt(runtime, session, {"CONTEXT_GUARD_HOME": str(override)})
                self.assertEqual(response, passing[runtime])
                self.assertNotIn(session, self.sessions(Kit.home, runtime))
                key = state.session_key(runtime, session)
                self.assertFalse(list((Kit.home / ".context-guard/state" / runtime).glob(f"{key}*")))
        self.assertFalse(override.exists())


class InstallTests(unittest.TestCase):
    """Install registers kit commands, preserves user files and leaves doctor clean."""

    def test_every_runtime_registers_kit_commands_on_the_discovered_interpreter(self) -> None:
        python = Kit.home / ".context-guard/kit/.venv/bin/python"
        version = subprocess.run(
            [str(python), "-c", "import sys; print(sys.version_info >= (3, 14))"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(version, "True")
        expected = {
            "cursor": {"sessionStart", "beforeSubmitPrompt", "preToolUse", "postToolUse", "stop"},
            "codex": {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"},
            "claude": {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "PreCompact", "PostCompact"},
        }
        for runtime, events in expected.items():
            with self.subTest(runtime=runtime):
                self.assertEqual(set(Kit.commands[runtime]), events)
                self.assertTrue(all(command.startswith(str(python)) for command in Kit.commands[runtime].values()))

    def test_user_hooks_and_settings_survive_install(self) -> None:
        cursor = json.loads((Kit.home / ".cursor/hooks.json").read_text())["hooks"]
        self.assertEqual(cursor["afterFileEdit"], [{"command": "./format.sh"}])
        self.assertIn({"command": "say done"}, cursor["stop"])
        claude = json.loads((Kit.home / ".claude/settings.json").read_text())
        self.assertEqual((claude["model"], claude["permissions"]), ("sonnet", {"allow": ["Bash(ls:*)"]}))
        self.assertTrue((Kit.home / ".codex/config.toml").read_text().startswith(USER_FILES[".codex/config.toml"]))
        for name in (".codex/AGENTS.md", ".claude/CLAUDE.md"):
            self.assertEqual((Kit.home / name).read_text(), USER_FILES[name])

    def test_doctor_is_clean(self) -> None:
        result = cli(Kit.home, "doctor", "--runtime", "all")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            sorted(result.stdout.splitlines()), sorted(f"[{name}] ok" for name in ("kit", *harness.RUNTIMES))
        )


class CursorInstalledTests(InstalledCase):
    """The installed Cursor hooks walk note, refresh, block and resend."""

    runtime = "cursor"

    def ids(self) -> dict:
        return {"conversation_id": self.session, "session_id": self.session}

    def chat(self, tokens: int) -> None:
        harness.write_cursor_chat(self.home, self.session, tokens, 200_000)

    def fire(self, fixture: str, **overrides: object) -> dict:
        return super().fire(fixture, cwd=self.home / ".cursor", **overrides)

    def test_registered_commands_list_note_refresh_block_and_resend(self) -> None:
        self.assertIn(str(self.handoff_file()), self.fire("sessionStart-root")["additional_context"])
        self.chat(120_000)
        self.assertEqual(self.fire("beforeSubmitPrompt-root", prompt="Ship the parser"), {"continue": True})
        self.assertTrue(self.fire("postToolUse-shell")["additional_context"].startswith("Context check"))
        self.assertEqual(self.fire("preToolUse-task"), {})
        call = harness.read_fixture("cursor", "preToolUse-task")["payload"]["tool_use_id"]
        self.assertEqual(self.fire("postToolUse-task", tool_use_id=call), {})
        self.assertTrue(self.fire("stop-completed", loop_count=0)["followup_message"].startswith("[context-guard]"))
        self.chat(190_000)
        blocked = self.fire("beforeSubmitPrompt-root", prompt="Next step")
        self.assertFalse(blocked["continue"])
        self.assertIn("190k", blocked["user_message"])
        self.assertEqual(self.fire("beforeSubmitPrompt-root", prompt=" Next step "), {"continue": True})
        self.assertEqual(
            self.walked(), ["note", "note_delivered", "subagent_spawn", "handoff_requested", "block", "resend"]
        )


class CodexInstalledTests(InstalledCase):
    """The installed Codex hooks walk note, refresh, block and resend."""

    runtime = "codex"

    def setUp(self) -> None:
        super().setUp()
        self.rollout = self.home / f".codex/sessions/2026/10/18/rollout-2026-10-18T09-00-00-{self.session}.jsonl"

    def ids(self) -> dict:
        return {"session_id": self.session, "transcript_path": str(self.rollout)}

    def tokens(self, count: int) -> None:
        harness.write_rollout(self.rollout, count, CODEX_WINDOW)

    def context(self, response: dict) -> str:
        return response["hookSpecificOutput"]["additionalContext"]

    def test_registered_commands_list_note_refresh_block_and_resend(self) -> None:
        handoff = self.handoff_file()
        context = self.context(self.fire("SessionStart-startup"))
        self.assertIn(str(handoff), context)
        self.assertIn("spawn_agent", context)
        self.tokens(160_000)
        self.assertTrue(self.context(self.fire("UserPromptSubmit-root", prompt="Ship it")).startswith("Context check"))
        self.assertEqual(self.fire("PostToolUse-bash", tool_input={"command": "echo done"}), {})
        self.assertTrue(self.fire("Stop-root")["reason"].startswith("[context-guard]"))
        self.tokens(250_000)
        blocked = self.fire("UserPromptSubmit-root", prompt="Next step")
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("250k", blocked["reason"])
        self.assertEqual(self.fire("UserPromptSubmit-root", prompt="Next step"), {})
        self.assertEqual(self.walked(), ["note", "note_delivered", "handoff_requested", "block", "resend"])


class ClaudeInstalledTests(InstalledCase):
    """The installed Claude Code hooks walk note, refresh, block and resend."""

    runtime = "claude"

    def setUp(self) -> None:
        super().setUp()
        self.transcript = self.home / f".claude/projects/-dev-project/{self.session}.jsonl"

    def ids(self) -> dict:
        return {"session_id": self.session, "transcript_path": str(self.transcript)}

    def tokens(self, count: int) -> None:
        harness.append_transcript(self.transcript, harness.assistant_usage(count, "claude-sonnet-5-5"))

    def context(self, response: dict) -> str:
        return response["hookSpecificOutput"]["additionalContext"]

    def test_registered_commands_list_note_refresh_block_and_resend(self) -> None:
        self.assertIn(str(self.handoff_file()), self.context(self.fire("SessionStart-startup")))
        self.tokens(120_000)
        self.assertTrue(self.context(self.fire("UserPromptSubmit-root", prompt="Ship it")).startswith("Context check"))
        self.assertEqual(self.fire("PostToolUse-bash", tool_input={"command": "echo done"}), {})
        self.assertTrue(self.fire("Stop-root")["reason"].startswith("[context-guard]"))
        self.tokens(190_000)
        blocked = self.fire("UserPromptSubmit-root", prompt="Next step")
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("190k", blocked["reason"])
        self.assertEqual(self.fire("UserPromptSubmit-root", prompt="Next step"), {})
        self.assertEqual(self.fire("PreCompact-auto"), {})
        self.assertEqual(self.fire("PostCompact-auto"), {})
        self.assertEqual(self.walked(), ["note", "note_delivered", "handoff_requested", "block", "resend"])


class UninstallTests(unittest.TestCase):
    """Uninstall restores user files byte for byte and removes everything the kit added."""

    def install_then_uninstall(self, files: dict[str, str]) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name).resolve()
        seed(home, files)
        self.assertEqual(cli(home, "install", "--runtime", "all").returncode, 0)
        project = home / "dev" / "project"
        (project / ".git").mkdir(parents=True)
        for runtime, fixture in (
            ("cursor", "sessionStart-root"),
            ("codex", "SessionStart-startup"),
            ("claude", "SessionStart-startup"),
        ):
            data = harness.payload(runtime, fixture, home, project)
            command = registered(home)[runtime][data["hook_event_name"]]
            self.assertEqual(harness.run_hook(command, data, home, project).returncode, 0)
        result = cli(home, "uninstall", "--runtime", "all")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return home

    def assert_restored(self, home: Path, files: dict[str, str]) -> None:
        for name, text in files.items():
            with self.subTest(file=name):
                self.assertEqual((home / name).read_bytes(), text.encode())
        self.assertFalse((home / ".context-guard/kit").exists())
        leftovers = [
            str(p.relative_to(home))
            for runtime in (".cursor", ".codex", ".claude")
            for p in (home / runtime).rglob("*")
            if p.is_file() and p.relative_to(home).as_posix() not in files
        ]
        self.assertEqual(leftovers, [])

    def test_uninstall_restores_user_files_byte_identical(self) -> None:
        self.assert_restored(self.install_then_uninstall(USER_FILES), USER_FILES)

    def test_uninstall_restores_hand_formatted_user_files_byte_identical(self) -> None:
        files = {
            ".cursor/hooks.json": '{"version": 1,\n    "hooks": {"stop": [{"command": "say done"}]}}',
            ".claude/settings.json": '{\n    "model": "opus"\n}',
            ".codex/config.toml": 'model = "gpt-6.1-sol"',
        }
        self.assert_restored(self.install_then_uninstall(files), files)

    def test_install_into_an_empty_home_leaves_nothing_after_uninstall(self) -> None:
        self.assert_restored(self.install_then_uninstall({}), {})


if __name__ == "__main__":
    unittest.main()
