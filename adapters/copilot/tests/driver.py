"""Copilot recorded payload translation with independent SDK response decoding."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from adapters.copilot.tests import native_lifecycle, responses
from tests import harness
from tests.driver_support import Operations, Outcome


class Driver(Operations):
    """Translate semantic actions into recorded Copilot hooks in temporary homes."""

    runtime = "copilot"
    response_runtime = "copilot"
    window = 272_000
    native_file = ".copilot/hooks/context-guard.json"
    grouped_hooks = False
    command_key = "bash"
    fixtures: ClassVar[dict[str, str]] = {
        "start": "sessionStart-new",
        "resume": "sessionStart-resume",
        "prompt": "userPromptSubmitted-root",
        "stop": "agentStop-root",
        "tool": "postToolUse-view-root",
        "read": "postToolUse-view-root",
        "spawn": "postToolUse-task-root",
        "leaf": "postToolUse-bash-subagent",
        "leaf_start": "subagentStart-plugin-child",
        "leaf_stop": "subagentStop-plugin-child",
        "compact": "userPromptSubmitted-root",
        "precompact": "preCompact-manual",
        "shell_write": "postToolUse-bash-root",
        "write": "postToolUse-apply_patch-add",
    }

    def __init__(self, home: Path, project: Path, session: str) -> None:
        super().__init__(home, project, session)
        self.transcript = home / ".copilot/session-state" / session / "events.jsonl"
        self.native_event = "userPromptSubmitted"
        self.native_data: dict = {}

    def event_name(self, action: str, data: dict) -> str:
        """Read the action's recorded native event without adding input fields.

        Args:
            action: Semantic action being invoked.
            data: Original materialized hook input.

        Returns:
            The event recorded alongside the payload.
        """
        self.native_event = harness.read_fixture(self.runtime, self.fixtures[action])["event"]
        self.native_data = data
        return self.native_event

    def payload(self, action: str, **overrides: object) -> dict:
        """Materialize recorded native identities and tool arguments.

        Args:
            action: Shared semantic action.
            **overrides: Explicit native field overrides, plus semantic role/path.

        Returns:
            Native payload retaining the recorded event's shape.
        """
        fields = dict(overrides)
        role = fields.pop("role", "scout")
        path = fields.pop("path", str(self.home / ".context-guard/handoffs/task.md"))
        data = harness.payload(
            self.runtime, self.fixtures[action], self.home, self.project, sessionId=self.session, cwd=str(self.project)
        )
        if "transcriptPath" in data or action == "leaf":
            data["transcriptPath"] = str(self.transcript)
        if action in {"leaf", "leaf_stop", "leaf_start"}:
            data.update(agentId=self.agent, agentType=role, parentAgentId=self.session)
            if action == "leaf":
                data["sessionId"] = self.agent
        else:
            for field in ("agentId", "agentType", "parentAgentId"):
                data.pop(field, None)
        if action == "spawn":
            data["toolArgs"] = {**data["toolArgs"], "agent_type": role, "prompt": "inspect"}
        elif action == "shell_write":
            data["toolArgs"] = {"command": f"printf x > '{path}'"}
        elif action == "write":
            data["toolArgs"] = (
                "\n".join(
                    f"*** Add File: {path}" if line.startswith("*** Add File:") else line
                    for line in data["toolArgs"].splitlines()
                )
                + "\n"
            )
        elif action == "read":
            data["toolArgs"] = {**data["toolArgs"], "path": path}
        if action == "prompt":
            data["prompt"] = "Continue implementation"
        data.update(fields)
        return data

    def fire(self, action: str, **overrides: object) -> Outcome:
        """Execute native hooks, observing completed compaction on the next prompt.

        Args:
            action: Shared semantic action.
            **overrides: Explicit native payload overrides.

        Returns:
            Independently validated response from the real hook process.

        Raises:
            AssertionError: The recorded compaction event lacks its native identity.
        """
        if action == "compact":
            record = copy.deepcopy(harness.read_fixture(self.runtime, "events-compaction-complete")["record"])
            if not record.get("id"):
                msg = "recorded completed compaction lacks its native event identity"
                raise AssertionError(msg)
            harness.append_transcript(self.transcript, record)
        if action == "leaf":
            self.fire("leaf_start", role=overrides.get("role", "scout"))
        return super().fire(action, **overrides)

    def tokens(self, count: int, *, leaf: bool = False) -> None:
        """Append a recorded native usage event, preserving per-model window data.

        Args:
            count: Explicit input-token count including cached tokens.
            leaf: Write an isolated child event log instead of the parent.
        """
        name = "events-subagent-usage-record" if leaf else "events-usage-record"
        record = copy.deepcopy(harness.read_fixture(self.runtime, name)["record"])
        record["data"]["usage"].update(inputTokens=count, maxPromptTokens=self.window)
        if leaf:
            record["agentId"] = self.agent
        harness.append_transcript(self.transcript, record)

    def decode(self, raw: dict) -> Outcome:
        """Decode using independent SDK contracts and the original native input.

        Args:
            raw: Schema-validated native response.

        Returns:
            Native refusal status and model-facing context.
        """
        return responses.decode(self.native_event, self.native_data, raw)

    def seed_user_files(self) -> dict[Path, bytes]:
        """Seed a separate user hook file beside the wholly kit-owned hook file.

        Returns:
            Exact original bytes that install and uninstall must preserve.
        """
        path = self.home / ".copilot/hooks/user.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"version":1,"hooks":{"agentStop":[{"type":"command","bash":"true"}]}}\n')
        return {path: path.read_bytes()}

    def configure_access(self) -> None:
        """Invoke the public workspace helper with isolated native configuration.

        Raises:
            AssertionError: The public setup command fails.
        """
        (self.home / ".context-guard/handoffs").mkdir(parents=True, exist_ok=True)
        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(harness.ROOT / "adapters/copilot/access.py"),
                "install",
                "--home",
                str(self.home),
                "--workspace",
                str(self.project),
            ],
            env=harness.hook_env(self.home),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(result)

    def access_grants(self) -> set[str]:
        """Decode native workspace directory grants independently of production code.

        Returns:
            Canonical paths admitted for this workspace only.
        """
        path = self.home / ".copilot/permissions-config.json"
        data = json.loads(path.read_text())
        return set(data.get("locations", {}).get(str(self.project.resolve()), {}).get("allowed_directories", []))

    def role_models(self) -> dict[str, str]:
        """Read model declarations using Copilot's compound agent-file suffix.

        Returns:
            Installed role names mapped to their declared native model.
        """
        return {name.removesuffix(".agent"): model for name, model in super().role_models().items()}

    def wake(self) -> dict:
        """Replay measured native ACP lifecycle evidence without starting a host.

        Returns:
            Parent-start and model-visible result evidence independently computed
            from captured native journal events and actual model-output chunks.
        """
        return native_lifecycle.observed_wake(harness.read_fixture(self.runtime, "lifecycle/idle-parent-wake"))

    def silence(self) -> Outcome:
        """Deliver a once-only notice from recorded child activity under a controlled clock.

        Returns:
            The parent's actual native tool response after the child goes silent.
        """
        self.now = 100.0
        self.fire("leaf_start", role="investigator")
        self.now += 601
        return self.fire("tool")

    def runner_contract(self) -> dict[str, bool]:
        """Exercise the runner with account-selection reads confined to a temporary home.

        Returns:
            Shared model pinning, classifier and calibration assertions.
        """
        with patch.dict(os.environ, {"COPILOT_HOME": str(self.home / ".copilot")}):
            return super().runner_contract()

    def arm_recorded(self, data: dict, *, subagent: bool) -> None:
        """Write recorded root or child usage above the configured block threshold.

        Args:
            data: Materialized recorded native hook input.
            subagent: Whether the capture originated in a child.
        """
        self.session = (data.get("parentAgentId") if subagent else None) or data.get("sessionId", self.session)
        self.transcript = Path(
            data.get("transcriptPath") or self.home / ".copilot/session-state" / self.session / "events.jsonl"
        )
        self.tokens(int(self.window * 0.98))
        if subagent:
            self.agent = data["agentId"]
            self.tokens(int(self.window * 0.8), leaf=True)
