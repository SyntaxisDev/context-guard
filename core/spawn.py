"""`subagent_spawn` detail shared by every adapter."""

from collections.abc import Callable, Mapping

from core.render import RenderError, load_roles


def role_default_tier(role: str | None) -> str | None:
    """Return a role's default tier from the shipped roles.

    Args:
        role: Role name, or None.

    Returns:
        The tier, or None for no role, an unknown role or unreadable roles.
    """
    if not role:
        return None
    try:
        found = load_roles().get(role)
    except RenderError:
        return None
    return found.default_tier if found else None


def default_detail(
    role: str | None, tiers: Mapping, profile: Callable[[Mapping], tuple[str | None, str | None]]
) -> dict:
    """A spawn that names no model runs its role's default tier: model and effort are that tier's profile.

    Args:
        role: The spawned role, or None.
        tiers: The adapter's tier profiles by tier name.
        profile: Reads (model, effort) from one tier profile.

    Returns:
        Spawn detail with role, tier, model and effort; unknown values are None.
    """
    tier = role_default_tier(role)
    found = tiers.get(tier) if tier else None
    model, effort = profile(found) if isinstance(found, Mapping) else (None, None)
    return {"role": role, "tier": tier, "model": model, "effort": effort}
