"""Codex native fixture translation and independent outcome decoding."""

import json
import sys
from pathlib import Path
from typing import ClassVar

from tests import harness
from tests.driver_support import Operations, Outcome


class Driver(Operations):
    """Translate shared actions to recorded Codex hooks and rollout telemetry."""

    runtime = "codex"
    response_runtime = "codex"
    toml_config = True
    window = 258_400
    native_file = ".codex/hooks.json"
    fixtures: ClassVar[dict[str, str]] = {
        "start": "SessionStart-startup",
        "resume": "SessionStart-resume",
        "compact": "SessionStart-compact",
        "prompt": "UserPromptSubmit-root",
        "tool": "PostToolUse-bash",
        "stop": "Stop-root",
        "spawn": "PostToolUse-spawn-agent",
        "write": "PostToolUse-apply-patch",
        "shell_write": "PostToolUse-bash",
        "leaf": "PostToolUse-subagent-bash",
        "read": "PostToolUse-bash",
        "leaf_stop": "SubagentStop-subagent",
    }

    def payload(self, action: str, **overrides: object) -> dict:
        """Materialize the recorded action and translate native tool arguments.

        Args:
            action: Semantic action to translate.
            **overrides: Explicit native payload overrides.

        Returns:
            The observed native result.
        """
        fields = dict(overrides)
        role = fields.pop("role", "scout")
        path = fields.pop("path", str(self.home / ".context-guard/handoffs/recorded-fixture-task-0123456789abcdef.md"))
        data = harness.payload(
            self.runtime,
            self.fixtures[action],
            self.home,
            self.project,
            session_id=self.session,
            transcript_path=str(self.transcript),
            agent_id=None,
            agent_type=None,
        )
        if action in {"tool", "leaf"}:
            data.update(tool_name="exec_command", tool_input={"cmd": "true"})
        elif action == "spawn":
            data.update(tool_name="spawn_agent", tool_input={"agent_type": role, "fork_turns": "none"})
        elif action == "write":
            patch = f"*** Begin Patch\n*** Add File: {path}\n+# Task\n*** End Patch\n"
            data.update(tool_name="apply_patch", tool_input={"command": patch})
        elif action in {"shell_write", "read"}:
            command = f"printf x > '{path}'" if action == "shell_write" else f"cat '{path}'"
            data.update(tool_name="exec_command", tool_input={"cmd": command, "workdir": str(self.project)})
        if action in {"leaf", "leaf_stop"}:
            data.update(agent_id=self.agent, agent_type=role)
        if action == "stop":
            data.update(stop_hook_active=False)
        if action == "prompt":
            data["prompt"] = "Continue implementation"
        data.update(fields)
        return data

    def tokens(self, count: int, *, leaf: bool = False) -> None:
        """Append fixture-shaped usage to the selected native rollout.

        Args:
            count: Context tokens to record.
            leaf: Whether to write child telemetry.
        """
        path = harness.codex_child_rollout(self.transcript, self.agent) if leaf else self.transcript
        record = harness.read_fixture(self.runtime, "rollout-token-count")["record"]
        info = record["payload"]["info"]
        info["last_token_usage"].update(input_tokens=count, output_tokens=0, total_tokens=count, cached_input_tokens=0)
        info["model_context_window"] = self.window
        harness.append_transcript(path, record)

    def decode(self, raw: dict) -> Outcome:
        """Preserve native blocking separately from advisory context.

        Args:
            raw: Schema-validated native output.

        Returns:
            The observed native result.
        """
        context = raw.get("hookSpecificOutput", {}).get("additionalContext", "")
        text = context or raw.get("reason", "")
        channel = "additionalContext" if context else "reason" if raw.get("reason") else "none"
        return Outcome(raw, text, raw.get("decision") == "block", channel)

    def silence(self) -> Outcome:
        """Advance observed child activity until a root hook delivers a silence notice.

        Returns:
            The independently decoded root response.
        """
        self.now = 1_800_000_000.0
        self.tokens(1_000, leaf=True)
        self.fire("leaf", role="investigator")
        self.now += 12 * 60
        return self.fire("tool")

    def wake(self) -> dict:
        """Run the real waker against an idle rollout with its queue process stubbed.

        Returns:
            The independently inspected queue command evidence.

        Raises:
            AssertionError: When the isolated waker process fails.
        """
        records = [
            {"type": "event_msg", "payload": {"type": "task_complete"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "SubAgentActivity",
                        "kind": "completed",
                        "agent_thread_id": self.agent,
                        "agent_path": "/root/conformance",
                    },
                },
            },
        ]
        harness.append_transcript(self.transcript, *records)
        script = """
import json, sys, subprocess
from pathlib import Path
from unittest.mock import Mock
from adapters.codex import waker
from core import config
home, rollout, session, agent = sys.argv[1:]
settings = config.load_config(config.sources_for('codex', Path(home),
    Path('adapters/codex/config.json').resolve()), {})
job = waker.Job(rollout=rollout, root=session, agent=agent, codex_home=home+'/.codex',
    state_dir=home+'/.context-guard/state/codex', tail_bytes=2097152, events=settings['events'])
queued = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
result = waker.wake(job, sleep=lambda _: None, run=queued, which=lambda _: '/stub/codex')
argv = queued.call_args.args[0] if queued.called else []
print(json.dumps({'mechanism':'queue', 'parent_started':result.outcome == waker.SENT,
    'result_delivered': '--message' in argv and 'result is waiting' in argv[-1],
    'message': argv[-1] if argv else ''}))
"""
        run = harness.run_hook(
            [sys.executable, "-c", script, str(self.home), str(self.transcript), self.session, self.agent],
            {},
            self.home,
            harness.ROOT,
        )
        if (run.returncode, run.stderr) != (0, ""):
            raise AssertionError(run)
        return json.loads(run.stdout)

    def arm_recorded(self, data: dict, *, subagent: bool) -> None:
        """Write native usage beside the materialized recorded parent rollout.

        Args:
            data: Materialized recorded native payload.
            subagent: Whether the recorded payload belongs to a child.
        """
        path = Path(data["transcript_path"])
        harness.write_rollout(path, 250_000, self.window)
        if subagent:
            child = data.get("agent_transcript_path") or harness.codex_child_rollout(path, data["agent_id"])
            harness.write_rollout(Path(child), 160_000, self.window)
