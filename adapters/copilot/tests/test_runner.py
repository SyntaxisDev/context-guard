"""Copilot runner provider: isolated prompt-mode argv, scratch home, answer gating and fail-closed validation."""

import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters.copilot import hooks, runner
from core.runner.provider import Invocation, complete

ACCOUNT = {"host": "https://github.com", "login": "octo", "kind": "user"}


class RunnerTestCase(unittest.TestCase):
    """A temporary user home with a Copilot config that holds an account and a secret."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source-home"
        self.source.mkdir()
        config = {
            "loggedInUsers": [{**ACCOUNT, "token": "SECRET-TOKEN"}],
            "lastLoggedInUser": {**ACCOUNT, "token": "SECRET-TOKEN"},
            "trustedFolders": ["/private/place"],
        }
        (self.source / "config.json").write_text("// User settings\n" + json.dumps(config))
        self.scratch = self.base / "scratch"
        self.scratch.mkdir()
        self.config = hooks.load_config(self.base, environ={})
        self.model = self.config["tiers"]["cheap"]["model"]

    def call(self, prompt: str = "classify", schema: dict | None = None, effort: str = "low") -> Invocation:
        return runner.command(effort, self.source, "/bin/copilot", self.model, prompt, self.scratch, schema)


class CommandTests(RunnerTestCase):
    """The argv isolates the call and carries only what the CLI help documents."""

    def test_argv_pins_model_effort_and_disables_tools_hooks_history_and_updates(self) -> None:
        invocation = self.call()
        argv = invocation.argv
        program = argv.index("/bin/copilot")
        self.assertEqual(argv[:2], [runner.ENV_PROGRAM, f"COPILOT_HOME={self.scratch / 'copilot-home'}"])
        self.assertEqual(argv[program + 1 : program + 5], ["--model", self.model, "--reasoning-effort", "low"])
        for flag in ("--silent", "--no-auto-update", "--disable-builtin-mcps", "--no-custom-instructions"):
            self.assertIn(flag, argv)
        self.assertIn("--sync-session=off", argv)
        for kind in runner.DENIED_KINDS:
            self.assertIn(f"--deny-tool={kind}", argv)
        self.assertNotIn("-p", argv)
        self.assertNotIn("--allow-all", argv)
        self.assertEqual(invocation.input, "classify")
        self.assertIn("COPILOT_ALLOW_ALL", invocation.unset_env)

    def test_schema_is_prepended_to_the_prompt_because_the_cli_has_no_schema_flag(self) -> None:
        schema = {"type": "object"}
        text = self.call("plan", schema).input
        self.assertTrue(text.startswith(runner.SCHEMA_INSTRUCTION + json.dumps(schema)))
        self.assertTrue(text.endswith("plan"))

    def test_invalid_model_effort_or_prompt_fails_closed(self) -> None:
        for model in ("--allow-all", "a b", "", "m;x"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                runner.command("low", self.source, "/bin/copilot", model, "p", self.scratch, None)
        for effort, prompt in (("turbo", "p"), ("low", "  "), ("low", "a\0b")):
            with self.subTest(effort=effort, prompt=prompt), self.assertRaises(ValueError):
                self.call(prompt, effort=effort)


class ScratchHomeTests(RunnerTestCase):
    """The scratch Copilot home selects the account without copying anything else."""

    def test_only_account_selection_fields_are_copied_with_private_permissions(self) -> None:
        self.call()
        home = self.scratch / "copilot-home"
        config = json.loads((home / "config.json").read_text())
        self.assertEqual(config, {"loggedInUsers": [ACCOUNT], "lastLoggedInUser": ACCOUNT})
        text = "".join(path.read_text() for path in home.iterdir() if path.is_file())
        self.assertNotIn("SECRET-TOKEN", text)
        self.assertNotIn("trustedFolders", text)
        self.assertEqual(json.loads((home / "settings.json").read_text()), {"disableAllHooks": True})
        self.assertEqual(stat.S_IMODE((home / "config.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)

    def test_missing_or_corrupt_source_config_selects_no_account(self) -> None:
        self.assertEqual(runner.account_selection(None), {})
        self.assertEqual(runner.account_selection(self.base / "absent"), {})
        (self.source / "config.json").write_text("{not json")
        self.assertEqual(runner.account_selection(self.source), {})


class AnswerTests(RunnerTestCase):
    """An answer counts only when the session ran no tool."""

    def log(self, text: str) -> None:
        session = self.scratch / "copilot-home" / "session-state" / "s1"
        session.mkdir(parents=True)
        (session / "events.jsonl").write_text(text)

    def test_text_is_returned_trimmed(self) -> None:
        self.log('{"type":"session.start","data":{}}\n')
        self.assertEqual(runner.answer("  CONTINUE\n", self.scratch), "CONTINUE")
        self.assertIsNone(runner.answer("  \n", self.scratch))

    def test_missing_or_empty_native_evidence_discards_the_answer(self) -> None:
        self.assertIsNone(runner.answer("CONTINUE", self.scratch))
        self.log("")
        self.assertIsNone(runner.answer("CONTINUE", self.scratch))

    def test_corrupt_native_evidence_discards_the_answer(self) -> None:
        self.log("not json\n")
        self.assertIsNone(runner.answer("CONTINUE", self.scratch))

    def test_a_recorded_tool_execution_discards_the_answer(self) -> None:
        self.log('{"type":"tool.execution_start"}\n')
        self.assertIsNone(runner.answer("done", self.scratch))

    def test_an_unreadable_event_log_discards_the_answer(self) -> None:
        self.log("")
        (self.scratch / "copilot-home" / "session-state" / "s1" / "events.jsonl").write_bytes(b"\xff\xfe")
        self.assertIsNone(runner.answer("done", self.scratch))


class ProviderTests(RunnerTestCase):
    """The provider runs through the shared completion helper with a stubbed subprocess."""

    def test_complete_runs_the_stubbed_copilot_and_returns_its_answer(self) -> None:
        provider = runner.provider(self.config, self.base)
        done = subprocess.CompletedProcess([], 0, stdout="CONTINUE\n", stderr="")

        def recorded_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            home = Path(argv[1].removeprefix("COPILOT_HOME="))
            session = home / "session-state/stub"
            session.mkdir(parents=True)
            (session / "events.jsonl").write_text('{"type":"session.start","data":{}}\n')
            return done

        with (
            mock.patch("core.runner.provider.shutil.which", return_value="/bin/copilot"),
            mock.patch("core.runner.provider.processes.run", side_effect=recorded_run) as run,
        ):
            result = complete(provider, self.model, "classify", 10.0)
        self.assertEqual(result, "CONTINUE")
        self.assertEqual(run.call_args.args[0][0], runner.ENV_PROGRAM)
        self.assertNotIn("COPILOT_ALLOW_ALL", run.call_args.kwargs["env"])

    def test_a_failed_run_yields_no_answer(self) -> None:
        provider = runner.provider(self.config, self.base)
        failed = subprocess.CompletedProcess([], 1, stdout="CONTINUE", stderr="")
        with (
            mock.patch("core.runner.provider.shutil.which", return_value="/bin/copilot"),
            mock.patch("core.runner.provider.processes.run", return_value=failed),
        ):
            self.assertIsNone(complete(provider, self.model, "classify", 10.0))

    def test_services_use_the_cheap_tier_model(self) -> None:
        self.assertEqual(runner.services(self.base).cheap_model, self.model)


if __name__ == "__main__":
    unittest.main()
