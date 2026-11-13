"""Cursor native fixture translation and deferred response decoding."""

from typing import ClassVar

from adapters.cursor.tests.access_driver import AccessOperations
from adapters.cursor.tests.native_lifecycle import observed_wake
from tests import harness
from tests.driver_support import Operations, Outcome


class Driver(AccessOperations, Operations):
    """Translate shared actions to Cursor events and recorded composer telemetry."""

    runtime = "cursor"
    response_runtime = "cursor"
    grouped_hooks = False
    deferred_prompt = True
    runtime_cwd = True
    window = 200_000
    native_file = ".cursor/hooks.json"
    fixtures: ClassVar[dict[str, str]] = {
        "start": "sessionStart-root",
        "resume": "sessionStart-root",
        "compact": "preCompact-root",
        "compact_stop": "stop-compaction-completed",
        "compact_read": "postToolUse-recovery-read",
        "prompt": "beforeSubmitPrompt-root",
        "tool": "postToolUse-shell",
        "stop": "stop-completed",
        "spawn": "preToolUse-task",
        "write": "postToolUse-write",
        "shell_write": "postToolUse-shell",
        "leaf": "postToolUse-subagent-shell",
        "read": "postToolUse-read",
        "spawn_result": "postToolUse-task",
        "leaf_stop": "stop-completed",
    }

    def payload(self, action: str, **overrides: object) -> dict:
        """Translate parent/child identities and tool inputs using recorded events.

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
            conversation_id=self.session,
            session_id=self.session,
            parent_tool_call_id=None,
            tool_use_id=self.call,
            transcript_path=str(self.transcript),
        )
        if action in {"tool", "leaf"}:
            data.update(tool_name="Shell", tool_input={"command": "true"})
        elif action == "spawn":
            data.update(tool_input={"subagent_type": role, "prompt": "inspect", "description": "inspect"})
        elif action == "write":
            data.update(tool_input={"file_path": path, "content": "# Task"})
        elif action == "shell_write":
            data.update(tool_input={"command": f"printf x > '{path}'", "cwd": str(self.project)})
        elif action == "read":
            data.update(tool_input={"file_path": path})
        if action in {"leaf", "leaf_stop"}:
            data.update(conversation_id=self.agent, session_id=self.agent, parent_tool_call_id=self.call)
        if action == "stop":
            data.update(loop_count=0)
        if action == "prompt":
            data["prompt"] = "Continue implementation"
        data.update(fields)
        return data

    def tokens(self, count: int, *, leaf: bool = False) -> None:
        """Write recorded composer telemetry for the selected root or child.

        Args:
            count: Context tokens to record.
            leaf: Whether to write child telemetry.
        """
        harness.write_cursor_chat(self.home, self.agent if leaf else self.session, count, self.window)

    def decode(self, raw: dict) -> Outcome:
        """Distinguish native prompt vetoes, deferred context and stop followups.

        Args:
            raw: Schema-validated native output.

        Returns:
            The observed native result.
        """
        for channel in ("additional_context", "agent_message", "followup_message", "user_message"):
            if raw.get(channel):
                return Outcome(raw, raw[channel], raw.get("continue") is False, channel)
        return Outcome(raw, "", raw.get("continue") is False, "none")

    def fire(self, action: str, **overrides: object) -> Outcome:
        """Apply Cursor's native role association recipe before leaf activity.

        Args:
            action: Semantic action to translate.
            **overrides: Explicit native payload overrides.

        Returns:
            The observed native result.
        """
        if action == "compact":
            super().fire("compact", **overrides)
            generation = self.payload("compact")["generation_id"]
            super().fire("compact_stop", generation_id=generation)
            return super().fire("compact_read")
        role = overrides.get("role")
        if action == "leaf" and role and not getattr(self, "leaf_spawned", False):
            super().fire("spawn", role=role)
            self.leaf_spawned = True
        return super().fire(action, **overrides)

    def silence(self) -> Outcome:
        """Advance recorded background activity under a controlled clock to silence.

        Returns:
            The observed native result.
        """
        self.now = 1_800_000_000.0
        self.fire("spawn", role="investigator")
        self.now += 60
        self.fire("leaf")
        self.fire(
            "spawn_result",
            tool_output='{"status":"success","agentId":"agent-conformance","durationMs":5,"isBackground":true}',
        )
        self.now += 12 * 60
        return self.fire("tool")

    def wake(self) -> dict:
        """Verify idle-parent result delivery from a measured native capture.

        Returns:
            Independently derived offline wake evidence.
        """
        return observed_wake(harness.read_fixture("cursor", "lifecycle/idle-parent-wake"))

    def policy_text(self) -> str:
        """Read Cursor's installed always-applied native policy rule.

        Returns:
            The observed native result.

        Raises:
            AssertionError: When the native operation violates its contract.
        """
        result = self.install()
        if result.returncode:
            raise AssertionError(result.stderr)
        return (self.home / ".cursor/rules/context-guard.mdc").read_text()

    def corrupt_telemetry(self) -> None:
        """Corrupt the native composer database without creating policy state."""
        path = harness.cursor_db(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"invalid sqlite database")

    def arm_recorded(self, data: dict, *, subagent: bool) -> None:
        """Write recorded composer usage for the materialized native conversation.

        Args:
            data: Materialized recorded native payload.
            subagent: Whether the recorded payload belongs to a child.
        """
        harness.write_cursor_chat(self.home, data["conversation_id"], 160_000 if subagent else 195_000, self.window)
