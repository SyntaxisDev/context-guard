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
RUNTIMES = ("cursor", "codex", "claude")
BEGIN = "<!-- BEGIN generated: tiers -->"
END = "<!-- END generated: tiers -->"
BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)


def tier_table(runtime: str) -> str:
    tiers = json.loads((ROOT / "adapters" / runtime / "config.json").read_text())["tiers"]
    keys = list(next(iter(tiers.values())))
    rows = ["| tier | " + " | ".join(keys) + " |", "| --- |" + " --- |" * len(keys)]
    for tier, profile in tiers.items():
        rows.append(f"| {tier} | " + " | ".join(f"`{profile[key]}`" for key in keys) + " |")
    return "\n".join([BEGIN, *rows, END])


def render(runtime: str, text: str) -> str:
    if not BLOCK.search(text):
        raise ValueError(f"docs/{runtime}.md has no generated tier markers")
    return BLOCK.sub(lambda _: tier_table(runtime), text)


def stale(runtime: str) -> bool:
    path = ROOT / "docs" / f"{runtime}.md"
    return render(runtime, path.read_text()) != path.read_text()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    status = 0
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
