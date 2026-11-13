"""Discover adapter declarations without importing hooks, mappings or test drivers."""

import importlib
import re
from collections.abc import Mapping
from pathlib import Path

import adapters
from core.features import Adapter, Feature, Gap, GapKind

ROOT = Path(__file__).resolve().parents[1]


def validate_documentation(runtime: str, link: str) -> None:
    """Validate a gap link against its runtime document and heading anchors.

    Args:
        runtime: The declaring runtime name.
        link: Repository-relative documentation link with a heading fragment.

    Raises:
        ValueError: The target document or anchor does not exist.
    """
    path, separator, anchor = link.partition("#")
    target = ROOT / "docs" / f"{runtime}.md"
    if path != f"docs/{runtime}.md" or not separator or not anchor or not target.is_file():
        msg = f"Invalid documentation link for {runtime}: {link}"
        raise ValueError(msg)
    text = target.read_text()
    headings = re.findall(r"^#{1,6} (.+?) *#* *$", text, re.MULTILINE)
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for heading in headings:
        base = re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-")
        count = counts.get(base, 0)
        anchors.add(f"{base}-{count}" if count else base)
        counts[base] = count + 1
    anchors.update(re.findall(r'<a\s+(?:id|name)=["\']([^"\']+)["\']', text))
    if anchor not in anchors:
        msg = f"Missing documentation anchor for {runtime}: {link}"
        raise ValueError(msg)


def validate_declaration(adapter: object, runtime: str) -> Adapter:
    """Validate one declaration and its documented feature gaps.

    Args:
        adapter: The registration module's ADAPTER value.
        runtime: The containing runtime package name.

    Returns:
        The validated adapter declaration.

    Raises:
        ValueError: The declaration or a feature gap is invalid.
    """
    module_name = f"adapters.{runtime}.adapter"
    if (
        not isinstance(adapter, Adapter)
        or adapter.name != runtime
        or adapter.hooks != f"adapters.{runtime}.hooks"
        or adapter.mapping != f"adapters.{runtime}.mapping"
        or not isinstance(adapter.gaps, Mapping)
    ):
        msg = f"Invalid adapter declaration: {module_name}"
        raise ValueError(msg)
    for feature, gap in adapter.gaps.items():
        if (
            not isinstance(feature, Feature)
            or not isinstance(gap, Gap)
            or not isinstance(gap.kind, GapKind)
            or not isinstance(gap.reason, str)
            or not gap.reason.strip()
            or not isinstance(gap.documentation, str)
            or not gap.documentation.strip()
        ):
            msg = f"Invalid feature gap in {module_name}"
            raise ValueError(msg)
    return adapter


def validate_adapter_documentation(adapter: Adapter) -> None:
    """Validate source documentation separately from installed-kit metadata.

    Args:
        adapter: Validated runtime declaration.

    Raises:
        ValueError: A runtime document or declared gap anchor is missing.
    """
    if not (ROOT / "docs" / f"{adapter.name}.md").is_file():
        msg = f"Missing runtime documentation: docs/{adapter.name}.md"
        raise ValueError(msg)
    for gap in adapter.gaps.values():
        validate_documentation(adapter.name, gap.documentation)


def discover() -> dict[str, Adapter]:
    """Discover explicit registration markers in alphabetical runtime order.

    Runtime packages with hooks must declare metadata. Marked packages are validated
    even when they lack hooks; arbitrary unmarked directories are ignored.

    Returns:
        Runtime names mapped to their adapter declarations.

    Raises:
        ValueError: A declaration is missing, cannot load, is duplicated or is invalid.
    """
    result: dict[str, Adapter] = {}
    for package in sorted(Path(adapters.__path__[0]).iterdir()):
        marker = package / "adapter.py"
        if not package.is_dir() or not (marker.is_file() or (package / "hooks.py").is_file()):
            continue
        module_name = f"adapters.{package.name}.adapter"
        if not marker.is_file():
            msg = f"Missing adapter declaration: {module_name}"
            raise ValueError(msg)
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            msg = f"Cannot load adapter declaration: {module_name}: {exc}"
            raise ValueError(msg) from exc
        adapter = validate_declaration(getattr(module, "ADAPTER", None), package.name)
        if adapter.name in result:
            msg = f"Duplicate adapter identity: {adapter.name}"
            raise ValueError(msg)
        result[adapter.name] = adapter
    return result
