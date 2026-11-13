#!/usr/bin/env python3
"""Regenerate the tier tables in docs/<runtime>.md from adapters/<runtime>/config.json.

`--check` exits 1 when a table is stale instead of rewriting it.
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.registry import discover, validate_adapter_documentation  # noqa: E402
from core.features import Feature  # noqa: E402

RUNTIMES = tuple(discover())
BEGIN = "<!-- BEGIN generated: tiers -->"
END = "<!-- END generated: tiers -->"
BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
SUPPORT_BEGIN = "<!-- BEGIN generated: declared-support -->"
SUPPORT_END = "<!-- END generated: declared-support -->"
SUPPORT_BLOCK = re.compile(re.escape(SUPPORT_BEGIN) + r".*?" + re.escape(SUPPORT_END), re.DOTALL)


def declared_support(text: str) -> str:
    """Replace the README's declared-support matrix using validated metadata.

    Args:
        text: The current README text, containing generated support markers.

    Returns:
        The README with current support declarations, independently of test results.

    Raises:
        ValueError: The README has no generated support markers.
    """
    if not SUPPORT_BLOCK.search(text):
        msg = "README.md has no generated declared-support markers"
        raise ValueError(msg)
    adapters = discover()
    for adapter in adapters.values():
        validate_adapter_documentation(adapter)
    rows = [
        SUPPORT_BEGIN,
        "Declared support (capability declarations, not test results).",
        "",
        "Gap links explain the reason and kind; universal guarantees cannot be opted out.",
        "",
        "| Capability | " + " | ".join(adapters) + " |",
        "| --- |" + " --- |" * len(adapters),
    ]
    for feature in Feature:
        cells = []
        for adapter in adapters.values():
            gap = adapter.gaps.get(feature)
            cells.append(f"[{gap.kind.value}]({gap.documentation})" if gap else "supported")
        rows.append(f"| `{feature.value}` | " + " | ".join(cells) + " |")
    return SUPPORT_BLOCK.sub(lambda _: "\n".join([*rows, SUPPORT_END]), text)


def tier_table(runtime: str) -> str:
    """Render an adapter's tier table between the generated markers.

    Args:
        runtime: The adapter name.

    Returns:
        The marked Markdown table with one row per tier and one column per profile key.
    """
    tiers = json.loads((ROOT / "adapters" / runtime / "config.json").read_text())["tiers"]
    keys = list(next(iter(tiers.values())))
    rows = ["| tier | " + " | ".join(keys) + " |", "| --- |" + " --- |" * len(keys)]
    for tier, profile in tiers.items():
        rows.append(f"| {tier} | " + " | ".join(f"`{profile[key]}`" for key in keys) + " |")
    return "\n".join([BEGIN, *rows, END])


def render(runtime: str, text: str) -> str:
    """Replace the generated tier block in a runtime's doc.

    Args:
        runtime: The adapter name.
        text: The doc's current text.

    Returns:
        The text with a freshly generated tier table.

    Raises:
        ValueError: When the text has no generated tier markers.
    """
    if not BLOCK.search(text):
        msg = f"docs/{runtime}.md has no generated tier markers"
        raise ValueError(msg)
    return BLOCK.sub(lambda _: tier_table(runtime), text)


def stale(runtime: str) -> bool:
    """Return whether a runtime's doc differs from its regenerated form.

    Args:
        runtime: The adapter name.

    Returns:
        True when the tier table is out of date.
    """
    path = ROOT / "docs" / f"{runtime}.md"
    return render(runtime, path.read_text()) != path.read_text()


def main(argv: list[str] | None = None) -> int:
    """Regenerate the stale tier tables, or with `--check` only report them.

    Args:
        argv: Command-line arguments, or None for `sys.argv`.

    Returns:
        1 when `--check` found a stale doc, else 0.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    status = 0
    readme = ROOT / "README.md"
    current = readme.read_text()
    rendered = declared_support(current)
    if current != rendered:
        if args.check:
            print("README.md is stale; run scripts/gen_docs.py", file=sys.stderr)
            status = 1
        else:
            readme.write_text(rendered)
    for runtime in RUNTIMES:
        path = ROOT / "docs" / f"{runtime}.md"
        if not stale(runtime):
            continue
        if args.check:
            print(f"{path.relative_to(ROOT)} is stale; run scripts/gen_docs.py", file=sys.stderr)
            status = 1
        else:
            path.write_text(render(runtime, path.read_text()))
    return status


if __name__ == "__main__":
    sys.exit(main())
