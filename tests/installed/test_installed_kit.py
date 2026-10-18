"""The installed kit end to end: `cli.py install --runtime all --home <tmp>` with real interpreter
discovery, then the exact command strings written to hooks.json / settings.json run as the runtime
would run them, fed recorded payloads, with telemetry (Cursor state.vscdb, Codex rollouts, Claude
transcripts) under the temporary home.
"""
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from core import policy, state
from tests import harness

CLI = harness.ROOT / "cli.py"
CODEX_WINDOW = 258_400
USER_FILES = {
    ".cursor/hooks.json": json.dumps({"version": 1, "hooks": {"afterFileEdit": [{"command": "./format.sh"}],
                                                             "stop": [{"command": "say done"}]}}, indent=2) + "\n",
    ".codex/hooks.json": json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}},
                                    indent=2) + "\n",
    ".codex/config.toml": 'model = "gpt-6.1-sol"\n\n[features]\nweb_search = true\n',
    ".codex/AGENTS.md": "# My global Codex notes\n",
    ".claude/settings.json": json.dumps({"model": "sonnet", "permissions": {"allow": ["Bash(ls:*)"]},
                                         "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}},
                                        indent=2) + "\n",
    ".claude/CLAUDE.md": "# My global Claude notes\n",
}


def cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI), *args, "--home", str(home)], capture_output=True, text=True,
                          env=harness.hook_env(home), timeout=300, check=False)


def seed(home: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text(text)


def kit_command(command: str, home: Path) -> bool:
    return str(home / ".context-guard" / "kit") in command


def registered(home: Path) -> dict[str, dict[str, str]]:
    """Per runtime, native event -> the exact command string the install registered."""
    cursor = json.loads((home / ".cursor/hooks.json").read_text())["hooks"]
    found = {"cursor": {event: entry["command"] for event, entries in cursor.items() for entry in entries
                        if kit_command(entry.get("command", ""), home)}}
    for runtime, path in (("codex", ".codex/hooks.json"), ("claude", ".claude/settings.json")):
        hooks = json.loads((home / path).read_text())["hooks"]
        found[runtime] = {event: hook["command"] for event, groups in hooks.items() for group in groups
                          for hook in group.get("hooks", []) if kit_command(hook.get("command", ""), home)}
    return found


class Kit:
    home: Path
    project: Path
    commands: dict[str, dict[str, str]]


def setUpModule():
    temporary = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(temporary.cleanup)
    Kit.home = Path(temporary.name).resolve()
    Kit.project = Kit.home / "dev" / "project"
    (Kit.project / ".git").mkdir(parents=True)
    seed(Kit.home, USER_FILES)
    result = cli(Kit.home, "install", "--runtime", "all")
    if result.returncode != 0:
        raise RuntimeError(f"install failed ({result.returncode}): {result.stdout}{result.stderr}")
    Kit.commands = registered(Kit.home)


class InstalledCase(unittest.TestCase):
    runtime = ""

    def setUp(self):
        self.home, self.project, self.session = Kit.home, Kit.project, str(uuid.uuid4())
        self.store = self.home / ".context-guard" / "handoffs"
        self.env: dict | None = None

    def fire(self, fixture: str, cwd: Path | None = None, **overrides) -> dict:
        data = harness.payload(self.runtime, fixture, self.home, self.project, **{**self.ids(), **overrides})
        command = Kit.commands[self.runtime][data["hook_event_name"]]
        run = harness.run_hook(command, data, self.home, cwd or self.project, self.env)
        self.assertEqual((run.returncode, run.stderr), (0, ""), command)
        response = run.response()
        self.assertEqual(harness.response_problems(self.runtime, data["hook_event_name"], response), [])
        return response

    def ids(self) -> dict:
        return {}

    def logged(self, session: str | None = None) -> list[str]:
        return [entry["event"] for entry in harness.events(self.home, self.runtime)
                if entry["session"] == (session or self.session)]

    def handoff_file(self) -> Path:
        return harness.seed_handoff(self.home, self.project, f"Installed {self.runtime} task {self.session[:8]}")


class HandoffTurns:
    """A note asks for a handoff update in its turn; only a real write into the store satisfies it."""
    note_tokens = 0

    def at(self, tokens: int) -> None: ...
    def prompt(self, text: str) -> None: ...
    def write_handoff(self, path: Path) -> None: ...
    def other_tool(self) -> None: ...
    def refresh(self) -> str | None: ...

    def requested(self) -> list[str]:
        return [entry["detail"]["source"] for entry in harness.events(self.home, self.runtime)
                if entry["session"] == self.session and entry["event"] == "handoff_requested"]

    def test_a_handoff_write_in_the_note_turn_means_no_refresh(self):
        self.at(self.note_tokens)
        self.prompt("Ship the parser")
        self.store.mkdir(parents=True, exist_ok=True)
        self.write_handoff(self.store / f"existing-task-{self.session[:8]}.md")
        self.assertIsNone(self.refresh())
        self.assertIn("handoff_written", self.logged())
        self.assertEqual(self.requested(), [])

    def test_a_note_turn_without_a_write_gets_one_refresh_naming_the_file(self):
        self.at(self.note_tokens)
        self.prompt("Ship the parser")
        self.other_tool()
        message = self.refresh()
        self.assertIn(f"{self.store}/ship-the-parser-", message)
        self.assertEqual(self.requested(), ["unwritten"])

    def test_refresh_never_repeats_in_consecutive_turns_without_token_growth(self):
        self.env = {"CONTEXT_GUARD_HANDOFF_REFRESH_MIN_SECONDS": "0"}
        self.at(self.note_tokens)
        self.prompt("Ship the parser")
        self.assertIsNotNone(self.refresh())
        for step in (1, 2):
            self.at(self.note_tokens + step * 1000)
            self.prompt(f"next step {step}")
            self.other_tool()
            self.assertIsNone(self.refresh())
        self.at(self.note_tokens + 61_000)
        self.prompt("later")
        self.assertIsNotNone(self.refresh())
        self.assertEqual(len(self.requested()), 2)


class HomeResolutionTests(unittest.TestCase):
    PROMPTS = {"cursor": "beforeSubmitPrompt-root", "codex": "UserPromptSubmit-root",
               "claude": "UserPromptSubmit-root"}

    def prompt(self, runtime: str, session: str, env: dict) -> dict:
        ids = {"conversation_id": session} if runtime == "cursor" else {}
        data = harness.payload(runtime, self.PROMPTS[runtime], Kit.home, Kit.project, session_id=session,
                               transcript_path=None, **ids)
        run = harness.run_hook(Kit.commands[runtime][data["hook_event_name"]], data, Kit.home, Kit.project, env)
        self.assertEqual((run.returncode, run.stderr), (0, ""))
        return run.response()

    def sessions(self, home: Path, runtime: str) -> set[str]:
        return {entry["session"] for entry in harness.events(home, runtime)}

    def test_installed_hooks_use_the_kit_home_also_when_context_guard_home_names_it(self):
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

    def test_installed_hooks_stay_inert_when_context_guard_home_names_another_home(self):
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
    def test_every_runtime_registers_kit_commands_on_the_discovered_interpreter(self):
        python = Kit.home / ".context-guard/kit/.venv/bin/python"
        version = subprocess.run([str(python), "-c", "import sys; print(sys.version_info >= (3, 14))"],
                                 capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(version, "True")
        expected = {"cursor": {"sessionStart", "beforeSubmitPrompt", "preToolUse", "postToolUse", "stop"},
                    "codex": {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop"},
                    "claude": {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "PreCompact", "PostCompact"}}
        for runtime, events in expected.items():
            with self.subTest(runtime=runtime):
                self.assertEqual(set(Kit.commands[runtime]), events)
                self.assertTrue(all(command.startswith(str(python)) for command in Kit.commands[runtime].values()))

    def test_user_hooks_and_settings_survive_install(self):
        cursor = json.loads((Kit.home / ".cursor/hooks.json").read_text())["hooks"]
        self.assertEqual(cursor["afterFileEdit"], [{"command": "./format.sh"}])
        self.assertIn({"command": "say done"}, cursor["stop"])
        claude = json.loads((Kit.home / ".claude/settings.json").read_text())
        self.assertEqual((claude["model"], claude["permissions"]), ("sonnet", {"allow": ["Bash(ls:*)"]}))
        self.assertTrue((Kit.home / ".codex/config.toml").read_text().startswith(USER_FILES[".codex/config.toml"]))
        for name in (".codex/AGENTS.md", ".claude/CLAUDE.md"):
            self.assertEqual((Kit.home / name).read_text(), USER_FILES[name])

    def test_doctor_is_clean(self):
        result = cli(Kit.home, "doctor", "--runtime", "all")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sorted(result.stdout.splitlines()),
                         sorted(f"[{name}] ok" for name in ("kit", *harness.RUNTIMES)))


class CursorInstalledTests(HandoffTurns, InstalledCase):
    runtime = "cursor"
    note_tokens = 120_000

    def ids(self) -> dict:
        return {"conversation_id": self.session, "session_id": self.session}

    def chat(self, tokens: int, conversation: str | None = None) -> None:
        harness.write_cursor_chat(self.home, conversation or self.session, tokens, 200_000)

    def fire(self, fixture: str, **overrides) -> dict:
        return super().fire(fixture, cwd=self.home / ".cursor", **overrides)

    def at(self, tokens: int) -> None:
        self.chat(tokens)

    def prompt(self, text: str) -> None:
        self.assertEqual(self.fire("beforeSubmitPrompt-root", prompt=text), {"continue": True})

    def write_handoff(self, path: Path) -> None:
        self.fire("postToolUse-write", tool_input={"file_path": str(path), "content": "# Task\n"})

    def other_tool(self) -> None:
        self.fire("postToolUse-shell")

    def refresh(self) -> str | None:
        return self.fire("stop-completed", loop_count=0).get("followup_message")

    def test_note_block_identical_resend_and_rearm(self):
        self.chat(120_000)
        self.assertEqual(self.fire("beforeSubmitPrompt-root", prompt="Ship the parser"), {"continue": True})
        self.assertTrue(self.fire("postToolUse-shell")["additional_context"].startswith("Context check"))
        self.chat(190_000)
        blocked = self.fire("beforeSubmitPrompt-root", prompt="Next step")
        self.assertFalse(blocked["continue"])
        self.assertTrue(blocked["user_message"].strip())
        self.assertIn("190k", blocked["user_message"])
        self.assertEqual(self.fire("beforeSubmitPrompt-root", prompt=" Next step "), {"continue": True})
        self.chat(20_000)
        self.assertEqual(self.fire("postToolUse-shell"), {})
        self.chat(120_000)
        self.assertEqual(self.fire("beforeSubmitPrompt-root", prompt="Again"), {"continue": True})
        self.assertIn("Context check", self.fire("postToolUse-grep")["additional_context"])
        names = self.logged()
        self.assertEqual([n for n in names if n != "prompt"], ["note", "note_delivered", "block", "resend", "note",
                                                               "note_delivered"])

    def test_subagent_tool_hooks_run_in_leaf_mode(self):
        sub = f"sub-{self.session}"
        self.chat(130_000, sub)
        first = self.fire("postToolUse-subagent-shell", conversation_id=sub, session_id=sub)
        self.assertEqual(first["additional_context"], policy.BUDGET_MESSAGE.format(tokens=130, budget=80))
        self.assertEqual(self.fire("postToolUse-subagent-read", conversation_id=sub, session_id=sub), {})
        self.assertEqual(self.logged(sub), ["subagent_budget"])

    def test_cli_chat_store_supplies_telemetry_without_a_gui_record(self):
        cli = f"cli-{self.session}"
        harness.write_cli_chat(self.home, cli, 190_000, 200_000)
        blocked = self.fire("beforeSubmitPrompt-root", conversation_id=cli, session_id=cli, prompt="Next step")
        self.assertFalse(blocked["continue"])
        self.assertIn("190k", blocked["user_message"])

    def test_session_start_lists_handoffs(self):
        handoff = self.handoff_file()
        context = self.fire("sessionStart-root")["additional_context"]
        self.assertIn(str(handoff), context)
        self.assertIn("(likely match)", context)

    def test_spawn_and_handoff_events(self):
        task = harness.read_fixture("cursor", "preToolUse-task")["payload"]["tool_input"]
        self.assertEqual(self.fire("preToolUse-task"), {})
        handoff = str(self.store / f"cursor-{self.session}.md")
        self.fire("postToolUse-write", tool_input={"file_path": handoff, "content": "# Task\n"})
        self.fire("postToolUse-read", tool_input={"file_path": handoff})
        entries = [e for e in harness.events(self.home, "cursor") if e["session"] == self.session]
        self.assertEqual([e["event"] for e in entries], ["subagent_spawn", "handoff_written", "handoff_read"])
        self.assertEqual(entries[0]["detail"]["role"], task["subagent_type"])
        self.assertEqual(entries[1]["detail"]["path"], handoff)

    def test_stop_requests_one_handoff_refresh(self):
        self.chat(170_000)
        self.assertTrue(self.fire("stop-completed", loop_count=0)["followup_message"].startswith("[context-guard]"))
        self.assertEqual(self.fire("stop-completed", loop_count=1), {})
        self.assertEqual(self.fire("stop-aborted"), {})
        self.assertEqual(self.logged(), ["handoff_requested"])


class CodexInstalledTests(HandoffTurns, InstalledCase):
    runtime = "codex"
    note_tokens = 150_000

    def setUp(self):
        super().setUp()
        self.rollout = self.home / f".codex/sessions/2026/10/18/rollout-2026-10-18T09-00-00-{self.session}.jsonl"

    def ids(self) -> dict:
        return {"session_id": self.session, "transcript_path": str(self.rollout)}

    def tokens(self, count: int, path: Path | None = None) -> None:
        harness.write_rollout(path or self.rollout, count, CODEX_WINDOW)

    def at(self, tokens: int) -> None:
        self.tokens(tokens)

    def prompt(self, text: str) -> None:
        self.fire("UserPromptSubmit-root", prompt=text)

    def write_handoff(self, path: Path) -> None:
        patch = f"*** Begin Patch\n*** Update File: {path}\n@@\n-old\n+new\n*** End Patch\n"
        self.fire("PostToolUse-apply-patch", tool_input={"command": patch})

    def other_tool(self) -> None:
        self.fire("PostToolUse-bash", tool_input={"command": "echo handoff updated"})

    def refresh(self) -> str | None:
        return self.fire("Stop-root").get("reason")

    def context(self, response: dict) -> str:
        return response["hookSpecificOutput"]["additionalContext"]

    def test_session_start_injects_policy_and_handoffs(self):
        handoff = self.handoff_file()
        context = self.context(self.fire("SessionStart-startup"))
        self.assertIn(str(handoff), context)
        self.assertIn("(likely match)", context)
        self.assertIn("spawn_agent", context)

    def test_note_block_identical_resend_and_compact_rearm(self):
        self.tokens(160_000)
        self.assertTrue(self.context(self.fire("UserPromptSubmit-root", prompt="Ship it")).startswith("Context check"))
        self.tokens(250_000)
        blocked = self.fire("UserPromptSubmit-root", prompt="Next step")
        self.assertEqual(blocked["decision"], "block")
        self.assertTrue(blocked["reason"].strip())
        self.assertIn("250k", blocked["reason"])
        self.assertEqual(self.fire("UserPromptSubmit-root", prompt="Next step"), {})
        self.context(self.fire("SessionStart-compact"))
        self.tokens(160_000)
        self.assertIn("Context check", self.context(self.fire("UserPromptSubmit-root", prompt="Continue")))
        self.assertEqual([n for n in self.logged() if n != "prompt"],
                         ["note", "note_delivered", "block", "resend", "note", "note_delivered"])

    def test_subagent_hooks_run_in_leaf_mode_on_their_own_rollout(self):
        agent = harness.read_fixture("codex", "PostToolUse-subagent-bash")["payload"]["agent_id"]
        self.tokens(250_000)
        self.tokens(130_000, harness.codex_child_rollout(self.rollout, agent))
        first = self.fire("PostToolUse-subagent-bash")
        self.assertIn("subagent budget", self.context(first))
        self.assertEqual(self.fire("UserPromptSubmit-subagent"), {})
        self.assertEqual(self.fire("SubagentStop-subagent", agent_transcript_path=None), {})
        self.assertEqual(self.fire("UserPromptSubmit-root", prompt="Parent prompt")["decision"], "block")
        self.assertEqual(self.logged(agent).count("subagent_budget"), 1)

    def test_spawn_and_handoff_events(self):
        spawn = harness.read_fixture("codex", "PostToolUse-spawn-agent")["payload"]["tool_input"]
        self.fire("PostToolUse-spawn-agent")
        handoff = self.store / f"codex-{self.session}.md"
        patch = f"*** Begin Patch\n*** Add File: {handoff}\n+# Task\n*** End Patch\n"
        self.fire("PostToolUse-apply-patch", tool_input={"command": patch})
        self.fire("PostToolUse-bash", tool_input={"command": f"sed -n '1,40p' {handoff}"})
        entries = [e for e in harness.events(self.home, "codex") if e["session"] == self.session]
        self.assertEqual([e["event"] for e in entries], ["subagent_spawn", "handoff_written", "handoff_read"])
        self.assertEqual(entries[0]["detail"], {"role": spawn["agent_type"], "tier": "cheap", "model": spawn["model"],
                                                "effort": spawn["reasoning_effort"]})

    def test_stop_requests_one_handoff_refresh(self):
        self.tokens(160_000)
        refresh = self.fire("Stop-root")
        self.assertEqual(refresh["decision"], "block")
        self.assertTrue(refresh["reason"].startswith("[context-guard]"))
        self.assertEqual(self.fire("Stop-root", stop_hook_active=True), {})
        self.assertEqual(self.logged(), ["handoff_requested"])


class ClaudeInstalledTests(HandoffTurns, InstalledCase):
    runtime = "claude"
    note_tokens = 120_000

    def setUp(self):
        super().setUp()
        self.transcript = self.home / f".claude/projects/-dev-project/{self.session}.jsonl"

    def ids(self) -> dict:
        return {"session_id": self.session, "transcript_path": str(self.transcript)}

    def tokens(self, count: int, path: Path | None = None) -> None:
        harness.append_transcript(path or self.transcript, harness.assistant_usage(count, "claude-sonnet-5-5"))

    def at(self, tokens: int) -> None:
        self.tokens(tokens)

    def prompt(self, text: str) -> None:
        self.fire("UserPromptSubmit-root", prompt=text)

    def write_handoff(self, path: Path) -> None:
        self.fire("PostToolUse-bash", tool_input={"command": f"cat > {path} <<'EOF'\n# Task\nEOF"})

    def other_tool(self) -> None:
        self.fire("PostToolUse-bash", tool_input={"command": "echo handoff updated"})

    def refresh(self) -> str | None:
        return self.fire("Stop-root").get("reason")

    def context(self, response: dict) -> str:
        return response["hookSpecificOutput"]["additionalContext"]

    def test_session_start_lists_handoffs(self):
        handoff = self.handoff_file()
        context = self.context(self.fire("SessionStart-startup"))
        self.assertIn(str(handoff), context)
        self.assertIn("(likely match)", context)

    def test_note_block_identical_resend_and_compaction_rearm(self):
        self.tokens(120_000)
        self.assertIn("200k", self.context(self.fire("UserPromptSubmit-root", prompt="Ship it")))
        self.tokens(190_000)
        blocked = self.fire("UserPromptSubmit-root", prompt="Next step")
        self.assertEqual(blocked["decision"], "block")
        self.assertEqual(self.fire("UserPromptSubmit-root", prompt="Next step"), {})
        self.assertEqual(self.fire("PreCompact-auto"), {})
        self.assertEqual(self.fire("PostCompact-auto"), {})
        self.assertEqual(self.fire("UserPromptSubmit-root", prompt="After compaction"), {})
        self.tokens(125_000)
        self.assertIn("Context check", self.context(self.fire("UserPromptSubmit-root", prompt="Later")))
        self.assertEqual([n for n in self.logged() if n != "prompt"],
                         ["note", "note_delivered", "block", "resend", "note", "note_delivered"])

    def test_subagent_hooks_run_in_leaf_mode_on_their_own_transcript(self):
        agent = harness.read_fixture("claude", "PostToolUse-subagent-bash")["payload"]["agent_id"]
        self.tokens(190_000)
        self.tokens(130_000, harness.claude_child_transcript(self.transcript, agent))
        self.assertIn("subagent budget", self.context(self.fire("PostToolUse-subagent-bash")))
        self.assertEqual(self.fire("PostToolUse-subagent-bash"), {})
        self.assertEqual(self.fire("UserPromptSubmit-root", prompt="Parent prompt")["decision"], "block")
        self.assertEqual(self.logged(agent).count("subagent_budget"), 1)

    def test_spawn_and_handoff_events(self):
        spawn = harness.read_fixture("claude", "PostToolUse-agent")["payload"]["tool_input"]
        self.fire("PostToolUse-agent")
        handoff = str(self.store / f"claude-{self.session}.md")
        self.fire("PostToolUse-write", tool_input={"file_path": handoff, "content": "# Task\n"})
        self.fire("PostToolUse-read", tool_input={"file_path": handoff})
        entries = [e for e in harness.events(self.home, "claude") if e["session"] == self.session]
        self.assertEqual([e["event"] for e in entries], ["subagent_spawn", "handoff_written", "handoff_read"])
        self.assertEqual(entries[0]["detail"]["role"], spawn["subagent_type"])

    def test_stop_requests_one_handoff_refresh(self):
        self.tokens(130_000)
        self.assertTrue(self.fire("Stop-root")["reason"].startswith("[context-guard]"))
        self.assertEqual(self.fire("Stop-root", stop_hook_active=True), {})
        self.assertEqual(self.logged(), ["handoff_requested"])


class UninstallTests(unittest.TestCase):
    def install_then_uninstall(self, files: dict[str, str]) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name).resolve()
        seed(home, files)
        self.assertEqual(cli(home, "install", "--runtime", "all").returncode, 0)
        project = home / "dev" / "project"
        (project / ".git").mkdir(parents=True)
        for runtime, fixture in (("cursor", "sessionStart-root"), ("codex", "SessionStart-startup"),
                                 ("claude", "SessionStart-startup")):
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
        leftovers = [str(p.relative_to(home)) for runtime in (".cursor", ".codex", ".claude")
                     for p in (home / runtime).rglob("*") if p.is_file() and p.relative_to(home).as_posix() not in files]
        self.assertEqual(leftovers, [])

    def test_uninstall_restores_user_files_byte_identical(self):
        self.assert_restored(self.install_then_uninstall(USER_FILES), USER_FILES)

    def test_uninstall_restores_hand_formatted_user_files_byte_identical(self):
        files = {".cursor/hooks.json": '{"version": 1,\n    "hooks": {"stop": [{"command": "say done"}]}}',
                 ".claude/settings.json": '{\n    "model": "opus"\n}',
                 ".codex/config.toml": 'model = "gpt-6.1-sol"'}
        self.assert_restored(self.install_then_uninstall(files), files)

    def test_install_into_an_empty_home_leaves_nothing_after_uninstall(self):
        self.assert_restored(self.install_then_uninstall({}), {})


if __name__ == "__main__":
    unittest.main()
