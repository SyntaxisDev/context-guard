"""Codex capability declaration."""

from core.features import Adapter

ADAPTER = Adapter(
    name="codex",
    hooks="adapters.codex.hooks",
    mapping="adapters.codex.mapping",
    gaps={},
)
