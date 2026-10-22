"""Shared wording of the notes that tell a parent about a subagent it cannot see."""

from pathlib import Path

from core.config import valid_text

RECOVERY = "It may have died or be stuck: check its progress file and transcript; cancel its jobs before re-spawning."


def subject(role: object, agent: object) -> str:
    """Name a subagent in a note by role and agent id, leaving out unknown parts.

    Args:
        role: The spawned role, or anything else when unknown.
        agent: The subagent's agent id, or anything else when unknown.

    Returns:
        Text such as "Subagent `implementer` (agent <id>)".
    """
    text = "Subagent" + (f" `{role}`" if valid_text(role) else "")
    return text + (f" (agent {agent})" if valid_text(agent) else "")


def silent_note(role: object, agent: object, transcript: Path | str | None, minutes: int) -> str:
    """Tell the parent a subagent has shown no activity for a while.

    Args:
        role: The spawned role, or anything else when unknown.
        agent: The subagent's agent id, or anything else when unknown.
        transcript: The subagent's transcript, or None when unknown.
        minutes: Whole minutes since its last activity or spawn.

    Returns:
        The note text.
    """
    where = f" Transcript: {transcript}." if transcript is not None else ""
    return f"[context-guard] {subject(role, agent)} has been silent for {minutes} minutes.{where} {RECOVERY}"
