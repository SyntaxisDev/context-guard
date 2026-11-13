"""Genie capability declaration."""

from core.features import Adapter, Feature, Gap, GapKind

ADAPTER = Adapter(
    name="genie",
    hooks="adapters.genie.hooks",
    mapping="adapters.genie.mapping",
    gaps={
        Feature.BACKGROUND_RESULT_WAKE: Gap(
            GapKind.RUNTIME_LIMIT,
            "No public command queues a new turn on an idle parent.",
            "docs/genie.md#unsupported",
        ),
        Feature.RUNNER: Gap(
            GapKind.RUNTIME_LIMIT,
            "The CLI cannot make isolated, model-pinned calls.",
            "docs/genie.md#unsupported",
        ),
    },
)
