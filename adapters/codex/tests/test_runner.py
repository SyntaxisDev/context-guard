"""Codex runner provider: codex exec argv, answer parsing and prompt-path wiring behind the flags."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters.codex import hooks, runner
from adapters.codex.tests.test_hooks import HookCase
from core import config as config_mod
from core.runner import service


class ProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.config = config_mod.load_config(config_mod.sources_for("codex", self.home, hooks.ADAPTER_CONFIG), {})

    def test_argv_runs_codex_exec_read_only_on_the_cheap_tier(self):
        cheap = self.config["tiers"]["cheap"]
        provider = runner.provider(self.config)
        call = provider.command("/bin/codex", cheap["model"], "classify this", self.home, None)
        argv = call.argv
        self.assertEqual(argv[:2], ["/bin/codex", "exec"])
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(argv[argv.index("--model") + 1], cheap["model"])
        settings = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"]
        self.assertIn(f'model_reasoning_effort="{cheap["effort"]}"', settings)
        self.assertIn("features.shell_tool=false", settings)
        self.assertIn("features.hooks=false", settings)
        self.assertNotIn("--output-schema", argv)
        self.assertEqual((argv[-1], call.input), ("-", "classify this"))
        planned = provider.command("/bin/codex", cheap["model"], "plan", self.home, {"type": "object"})
        schema = Path(planned.argv[planned.argv.index("--output-schema") + 1])
        self.assertEqual(json.loads(schema.read_text()), {"type": "object"})

    def test_answer_reads_the_last_message_and_rejects_tool_runs(self):
        (self.home / runner.ANSWER_FILE).write_text("MECHANICAL")
        message = json.dumps({"type": "item.completed", "item": {"type": "agent_message"}})
        tool = json.dumps({"type": "item.completed", "item": {"type": "command_execution"}})
        self.assertEqual(runner.answer(message, self.home), "MECHANICAL")
        self.assertIsNone(runner.answer(message + "\n" + tool, self.home))
        self.assertIsNone(runner.answer("not json", self.home))

    def test_services_use_the_cheap_tier_and_this_adapters_worker(self):
        services = runner.services(self.home)
        self.assertEqual(services.cheap_model, self.config["tiers"]["cheap"]["model"])
        self.assertEqual(services.worker_argv[1], str(Path(hooks.__file__).resolve()))


class WiringTests(HookCase):
    def prompt(self) -> tuple[dict, mock.Mock]:
        self.write_rollout(self.rollout, 200_000)
        with mock.patch.object(runner, "services") as build:
            build.return_value.prompt.return_value = service.Routing(True, "runner took it")
            response = self.event("UserPromptSubmit", prompt="commit and push")
        return response, build

    def test_disabled_runner_is_never_built(self):
        _, build = self.prompt()
        build.assert_not_called()

    def test_enabled_runner_routing_blocks_the_prompt(self):
        with mock.patch.dict(os.environ, {"CONTEXT_GUARD_RUNNER_ENABLED": "true"}):
            response, build = self.prompt()
        build.assert_called_once_with(self.home)
        self.assertEqual(response, {"decision": "block", "reason": "runner took it", "systemMessage": "runner took it"})


if __name__ == "__main__":
    unittest.main()
