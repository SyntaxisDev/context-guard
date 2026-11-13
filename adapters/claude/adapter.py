"""Claude Code capability declaration."""

from core.features import Adapter

ADAPTER = Adapter(name="claude", hooks="adapters.claude.hooks", mapping="adapters.claude.mapping", gaps={})
