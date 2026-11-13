"""Claude native fixture translation and independent response decoding."""

from pathlib import Path
from typing import ClassVar

from adapters.claude.tests.native_lifecycle import observed_wake
from tests import harness
from tests.driver_support import Operations, Outcome


class Driver(Operations):
    """Translate shared actions to Claude hooks and native assistant transcripts."""

    runtime = "claude"
    response_runtime = "claude"
    window = 200_000
    native_file = ".claude/settings.json"
    fixtures: ClassVar[dict[str, str]] = {
        "start": "SessionStart-startup",
        "resume": "SessionStart-startup",
        "compact": "SessionStart-compact",
        "prompt": "UserPromptSubmit-root",
        "tool": "PostToolUse-bash",
        "stop": "Stop-root",
        "spawn": "PostToolUse-agent",
        "write": "PostToolUse-write",
        "shell_write": "PostToolUse-bash",
        "leaf": "PostToolUse-subagent-bash",
        "read": "PostToolUse-read",
        "leaf_stop": "SubagentStop-subagent",
        "precompact": "PreCompact-auto",
        "postcompact": "PostCompact-auto",
    }

    def payload(self, action: str, **overrides: object) -> dict:
        """Translate semantic identities and tools on top of recorded payloads.

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
            data.update(tool_name="Bash", tool_input={"command": "true"})
        elif action == "spawn":
            data.update(tool_input={"subagent_type": role, "prompt": "inspect", "description": "inspect"})
        elif action == "write":
            data.update(tool_input={"file_path": path, "content": "# Task"})
        elif action == "shell_write":
            data.update(tool_input={"command": f"printf x > '{path}'"})
        elif action == "read":
            data.update(tool_input={"file_path": path})
        if action in {"leaf", "leaf_stop"}:
            data.update(agent_id=self.agent, agent_type=role)
        if action == "stop":
            data.update(stop_hook_active=False)
        if action == "resume":
            data.update(source="resume")
        if action == "prompt":
            data["prompt"] = "Continue implementation"
        data.update(fields)
        return data

    def tokens(self, count: int, *, leaf: bool = False) -> None:
        """Append a recorded assistant usage shape to the root or child transcript.

        Args:
            count: Context tokens to record.
            leaf: Whether to write child telemetry.
        """
        path = harness.claude_child_transcript(self.transcript, self.agent) if leaf else self.transcript
        harness.append_transcript(path, harness.assistant_usage(count, "claude-sonnet-5-5"))

    def decode(self, raw: dict) -> Outcome:
        """Decode model context, stop reasons and native prompt blocking independently.

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
        """Advance native background activity under a controlled clock to deliver silence.

        Returns:
            The observed native result.
        """
        self.now = 1_800_000_000.0
        self.fire(
            "spawn",
            role="investigator",
            tool_response={
                "isAsync": True,
                "status": "async_launched",
                "agentId": self.agent,
                "canContinueAgent": True,
            },
        )
        self.now += 12 * 60
        return self.fire("tool")

    def wake(self) -> dict:
        """Replay measured native completion delivery from the scrubbed lifecycle fixture.

        Returns:
            Independently derived idle-parent wake and model-visible child result evidence.
        """
        return observed_wake(harness.read_fixture("claude", "lifecycle/idle-parent-wake"))

    def policy_text(self) -> str:
        """Read the installed native policy rule that Claude loads with the session.

        Returns:
            The observed native result.

        Raises:
            AssertionError: When the native operation violates its contract.
        """
        result = self.install()
        if result.returncode:
            raise AssertionError(result.stderr)
        return (self.home / ".claude/rules/context-guard.md").read_text()

    def arm_recorded(self, data: dict, *, subagent: bool) -> None:
        """Materialize native root and child usage for exhaustive recorded payload cases.

        Args:
            data: Materialized recorded native payload.
            subagent: Whether the recorded payload belongs to a child.
        """
        path = Path(data["transcript_path"]) if data.get("transcript_path") else self.transcript
        harness.append_transcript(path, harness.assistant_usage(195_000, "claude-sonnet-5-5"))
        if subagent:
            harness.append_transcript(
                Path(data.get("agent_transcript_path") or harness.claude_child_transcript(path, data["agent_id"])),
                harness.assistant_usage(160_000, "claude-sonnet-5-5"),
            )
