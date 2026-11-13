"""Independent Copilot SDK hook output contracts, maintained from the recorded 1.0.107 d.ts.

The SDK UserPromptSubmitted, SessionStart, PostToolUse, AgentStop and SubagentStop
output interfaces define these fields. Unknown event families accept only empty pass
responses until their native output contract has been recorded and reviewed.
"""

from tests.driver_support import Outcome

FIELDS = {
    "sessionStart": {"additionalContext": str, "modifiedConfig": dict},
    "userPromptSubmitted": {
        "modifiedPrompt": str,
        "additionalContext": str,
        "suppressOutput": bool,
        "decision": str,
        "reason": str,
    },
    "userPromptTransformed": {"modifiedTransformedPrompt": str},
    "postToolUse": {"modifiedResult": dict, "additionalContext": str, "suppressOutput": bool},
    "postToolUseFailure": {"additionalContext": str},
    "notification": {"additionalContext": str},
    "agentStop": {"decision": str, "reason": str},
    "subagentStart": {"additionalContext": str},
    "subagentStop": {"decision": str, "reason": str, "modifiedResponse": str},
    "sessionEnd": {"suppressOutput": bool, "cleanupActions": list, "sessionSummary": str},
}


def problems(event: str, raw: object) -> list[str]:
    """Validate event-specific SDK response fields without production renderers.

    Args:
        event: Native event being answered.
        raw: Parsed native response.

    Returns:
        Contract violations, or an empty list for a valid native response.
    """
    if not isinstance(raw, dict):
        return ["response must be an object"]
    fields = FIELDS.get(event, {})
    errors = [f"unexpected {event} output field: {key}" for key in raw.keys() - fields.keys()]
    errors.extend(
        f"{key} has invalid type" for key, value in raw.items() if key in fields and not isinstance(value, fields[key])
    )
    if "decision" in raw:
        allowed = {"block", "allow"} if event == "subagentStop" else {"block"}
        if not isinstance(raw["decision"], str) or raw["decision"] not in allowed:
            errors.append("invalid decision")
    if event == "subagentStop":
        if raw.get("decision") == "block" and not raw.get("reason"):
            errors.append("subagent block requires a nonempty reason")
        if raw.get("decision") != "block" and "reason" in raw:
            errors.append("subagent allow cannot carry a reason")
    if "cleanupActions" in raw and isinstance(raw["cleanupActions"], list):
        errors.extend(
            "cleanupActions entries must be strings" for item in raw["cleanupActions"] if not isinstance(item, str)
        )
    return errors


def decode(event: str, data: dict, raw: dict) -> Outcome:
    """Distinguish prompt refusals from model-facing continuation and context.

    Args:
        event: Native event being answered.
        data: Original native hook input.
        raw: Independently validated native response.

    Returns:
        Native block status and model-facing text only.

    Raises:
        AssertionError: A transformed replacement removes the original prompt.
    """
    blocked = event == "userPromptSubmitted" and raw.get("decision") == "block"
    if blocked:
        return Outcome(raw, "", blocked=True, channel="user_warning", user_text=raw.get("reason", ""))
    if event in {"agentStop", "subagentStop"} and raw.get("decision") == "block":
        return Outcome(raw, raw.get("reason", ""), blocked=False, channel="continuation")
    if event == "userPromptTransformed" and "modifiedTransformedPrompt" in raw:
        original = data["transformedPrompt"]
        replacement = raw["modifiedTransformedPrompt"]
        if not replacement.startswith(original):
            msg = "transformed response removed the original prompt"
            raise AssertionError(msg)
        return Outcome(raw, replacement[len(original) :], blocked=False, channel="transformedPrompt")
    text = (
        raw.get("additionalContext", "")
        if event in {"sessionStart", "userPromptSubmitted", "postToolUse", "subagentStart"}
        else ""
    )
    return Outcome(raw, text, blocked=False, channel="additionalContext" if text else "none")
