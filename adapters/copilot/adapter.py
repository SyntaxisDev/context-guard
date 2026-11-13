"""GitHub Copilot CLI capability declaration."""

from core.features import Adapter

ADAPTER = Adapter(
    name="copilot",
    hooks="adapters.copilot.hooks",
    mapping="adapters.copilot.mapping",
    gaps={},
)
