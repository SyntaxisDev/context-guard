"""Claude runner provider: claude -p argv, answer parsing and prompt-path wiring behind the flags."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters.claude import hooks, runner
from adapters.claude.tests.test_hooks import HookTestCase
from core.runner import service


class ProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.config = hooks.load_config(self.home, environ={})

    def test_argv_runs_claude_print_without_tools_on_the_cheap_tier(self):
        model = runner.cheap_model(self.config)
        self.assertEqual(model, self.config["tiers"]["cheap"]["model"])
        call = runner.command("/bin/claude", model, "classify this", self.home, None)
        argv = call.argv
        self.assertEqual(argv[:4], ["/bin/claude", "--print", "--model", model])
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--settings") + 1], '{"disableAllHooks":true}')
        self.assertNotIn("--json-schema", argv)
        self.assertEqual((call.input, call.unset_env), ("classify this", ("CLAUDECODE",)))
        planned = runner.command("/bin/claude", model, "plan", self.home, {"type": "object"})
        self.assertEqual(json.loads(planned.argv[planned.argv.index("--json-schema") + 1]), {"type": "object"})

    def test_answer_prefers_structured_output_and_rejects_errors(self):
        plan = {"commands": [["git", "status"]]}
        self.assertEqual(json.loads(runner.answer(json.dumps({"structured_output": plan}), self.home)), plan)
        self.assertEqual(runner.answer(json.dumps({"result": "CONTINUE"}), self.home), "CONTINUE")
        for stdout in ("not json", json.dumps({"result": "x", "is_error": True}), "[]"):
            self.assertIsNone(runner.answer(stdout, self.home))

    def test_services_use_the_cheap_tier_and_this_adapters_worker(self):
        services = runner.services(self.home)
        self.assertEqual(services.cheap_model, self.config["tiers"]["cheap"]["model"])
        self.assertEqual(services.worker_argv[1], str(Path(hooks.__file__).resolve()))


class WiringTests(HookTestCase):
    def prompt(self, config: dict) -> tuple[dict, mock.Mock]:
        self.usage(190_000)
        payload = {"session_id": "s1", "cwd": str(self.project), "transcript_path": str(self.transcript),
                   "prompt": "commit and push"}
        with mock.patch.object(runner, "services") as build:
            build.return_value.prompt.return_value = service.Routing(True, "runner took it")
            response = hooks.handle("UserPromptSubmit", payload, config, self.home)
        return response, build

    def test_disabled_runner_is_never_built(self):
        _, build = self.prompt(self.config)
        build.assert_not_called()

    def test_enabled_runner_routing_blocks_the_prompt(self):
        response, build = self.prompt(hooks.load_config(self.home, environ={"CONTEXT_GUARD_RUNNER_ENABLED": "true"}))
        build.assert_called_once_with(self.home)
        self.assertEqual(response, {"decision": "block", "reason": "runner took it"})


if __name__ == "__main__":
    unittest.main()
