"""Cursor runner provider: cursor-agent argv, answer parsing and prompt-path wiring behind the flags."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters.cursor import hooks, runner
from adapters.cursor.tests.test_hooks import CursorHookCase
from core.runner import service

ENABLED = {"CONTEXT_GUARD_RUNNER_ENABLED": "true"}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.config = hooks.load(self.home, {})

    def test_argv_runs_cursor_agent_in_ask_mode_on_the_cheap_tier(self):
        model = runner.cheap_model(self.config)
        self.assertEqual(model, self.config["tiers"]["cheap"]["call"])
        call = runner.command("/bin/cursor-agent", model, "classify this", self.home, None)
        self.assertEqual(call.argv, ["/bin/cursor-agent", "-p", "--trust", "--mode", "ask", "--workspace",
                                     str(self.home), "--model", model, "--output-format", "json", "classify this"])
        self.assertIsNone(call.input)
        planned = runner.command("/bin/cursor-agent", model, "plan", self.home, {"type": "object"})
        self.assertTrue(planned.argv[-1].startswith("plan") and planned.argv[-1].endswith('{"type": "object"}'))

    def test_answer_reads_the_result_and_rejects_errors(self):
        self.assertEqual(runner.answer(json.dumps({"result": "MECHANICAL"}), self.home), "MECHANICAL")
        for stdout in ("not json", json.dumps({"result": "x", "is_error": True}), "[]", json.dumps({"result": 1})):
            self.assertIsNone(runner.answer(stdout, self.home))

    def test_services_use_the_cheap_tier_and_this_adapters_worker(self):
        services = runner.services(self.home)
        self.assertEqual(services.cheap_model, self.config["tiers"]["cheap"]["call"])
        self.assertEqual(services.worker_argv[1], str(Path(hooks.__file__).resolve()))
        self.assertEqual(services.provider.executable, "cursor-agent")


class WiringTests(CursorHookCase):
    def prompt(self, environ: dict) -> tuple[dict, mock.Mock]:
        self.tokens("c1", 120_000)
        with mock.patch.object(runner, "services") as build:
            build.return_value.prompt.return_value = service.Routing(True, "runner took it")
            response = hooks.run("beforeSubmitPrompt", {"conversation_id": "c1", "prompt": "commit and push",
                                                        "workspace_roots": [str(self.project)]}, self.home, environ)
        return response, build

    def test_disabled_runner_is_never_built(self):
        response, build = self.prompt({})
        build.assert_not_called()
        self.assertTrue(response["continue"])

    def test_enabled_runner_routing_blocks_the_prompt(self):
        response, build = self.prompt(ENABLED)
        build.assert_called_once_with(self.home)
        self.assertEqual(response, {"continue": False, "user_message": "runner took it"})


if __name__ == "__main__":
    unittest.main()
