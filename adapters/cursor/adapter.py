"""Cursor capability declaration."""

from core.features import Adapter

ADAPTER = Adapter(
    name="cursor",
    hooks="adapters.cursor.hooks",
    mapping="adapters.cursor.mapping",
    gaps={},
)
