#!/usr/bin/env python3
"""context-guard install|uninstall|doctor|status|package for Cursor, Codex and Claude Code.

Exit codes: 0 success, 1 conflicts were skipped or doctor found problems, 2 the command
could not run (bad arguments, missing adapter mapping, no usable interpreter, I/O error).
Hook trust is never written: review new hooks through each runtime's own controls.
`package` renders a self-contained plugin into --out instead of installing into a home;
its --set values are baked into the package rather than written to the user config.
"""

import argparse
import datetime as dt
import importlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TextIO

from core import config, events, install, kit, package
from core.install_spec import InstallSpec
from core.state import read_json

ROOT = Path(__file__).resolve().parent
RUNTIMES = ("cursor", "codex", "claude")
COMMANDS = ("install", "uninstall", "doctor", "status", "package")
EXIT_OK, EXIT_ISSUES, EXIT_ERROR = 0, 1, 2


class CliError(Exception):
    """A user-facing failure; main prints it and exits with EXIT_ERROR."""


def import_mapping(runtime: str) -> ModuleType:
    """Import a runtime's adapter mapping.

    Args:
        runtime: The runtime name.

    Returns:
        The `adapters.<runtime>.mapping` module.
    """
    return importlib.import_module(f"adapters.{runtime}.mapping")


def import_plugin(runtime: str) -> ModuleType:
    """Import a runtime's plugin package spec.

    Args:
        runtime: The runtime name.

    Returns:
        The `adapters.<runtime>.plugin` module.
    """
    return importlib.import_module(f"adapters.{runtime}.plugin")


@dataclass(frozen=True)
class Context:
    """What every command needs: the target home, the source checkout and injectable loaders.

    Attributes:
        home: The home directory to install into.
        source: The source checkout the kit and packages are built from.
        toolchain: Interpreter discovery and venv creation.
        load_mapping: Import a runtime's adapter mapping.
        out: Where user-facing lines are printed.
        load_plugin: Import a runtime's plugin package spec.
    """

    home: Path
    source: Path
    toolchain: kit.Toolchain
    load_mapping: Callable[[str], ModuleType]
    out: TextIO
    load_plugin: Callable[[str], ModuleType] = import_plugin

    @property
    def kit(self) -> Path:
        """The kit directory in the home."""
        return kit.kit_dir(self.home)

    def say(self, *lines: str) -> None:
        """Print lines to the command output.

        Args:
            *lines: The lines, each printed on its own.
        """
        for line in lines:
            print(line, file=self.out)

    def manifest_file(self, runtime: str) -> Path:
        """Locate a runtime's install manifest in the home.

        Args:
            runtime: The manifest name of the runtime, or "kit".

        Returns:
            The manifest path.
        """
        return install.manifest_path(self.home, runtime)

    def manifest(self, runtime: str) -> dict:
        """Read a runtime's validated install manifest.

        Args:
            runtime: The manifest name of the runtime, or "kit".

        Returns:
            The manifest, empty when not installed.
        """
        return install.read_manifest(self.manifest_file(runtime))


def runtime_sources(ctx: Context, runtime: str) -> config.ConfigSources:
    """Collect the config layers for one runtime.

    Args:
        ctx: The command context.
        runtime: The runtime name.

    Returns:
        The defaults, the adapter config when the source has one, and the user config.
    """
    adapter = ctx.source / "adapters" / runtime / "config.json"
    return config.sources_for(runtime, ctx.home, adapter if adapter.is_file() else None)


def runtime_config(ctx: Context, runtime: str) -> dict:
    """Load the effective config for one runtime.

    Args:
        ctx: The command context.
        runtime: The runtime name.

    Returns:
        The merged config.
    """
    return config.load_config(runtime_sources(ctx, runtime))


def runtime_spec(ctx: Context, runtime: str) -> InstallSpec:
    """Render one runtime's install spec through its adapter mapping.

    Args:
        ctx: The command context.
        runtime: The runtime name.

    Returns:
        The spec, which targets the kit and its venv python.

    Raises:
        CliError: The mapping is unavailable, cannot render, or returns another runtime's spec.
    """
    try:
        mapping = ctx.load_mapping(runtime)
        build = mapping.install_spec
    except (ImportError, AttributeError) as exc:
        msg = f"adapters/{runtime}/mapping.py is unavailable: {exc}"
        raise CliError(msg) from exc
    try:
        spec = build(ctx.home, ctx.kit, kit.venv_python(ctx.kit), runtime_config(ctx, runtime))
    except ValueError as exc:
        msg = f"{runtime}: cannot render the install: {exc}"
        raise CliError(msg) from exc
    if spec.runtime != runtime:
        msg = f"adapters/{runtime}/mapping.py returned a spec for {spec.runtime!r}"
        raise CliError(msg)
    return spec


def kit_spec(ctx: Context) -> InstallSpec:
    """Describe the kit install from the context's source checkout.

    Args:
        ctx: The command context.

    Returns:
        The kit's install spec.
    """
    return kit.kit_spec(ctx.kit, ctx.source)


def report(ctx: Context, plans: list[install.Plan], *, dry_run: bool) -> bool:
    """Print every plan.

    Args:
        ctx: The command context.
        plans: The plans to describe.
        dry_run: Whether to phrase changes as what would happen.

    Returns:
        Whether any conflict was skipped.
    """
    for plan in plans:
        ctx.say(*install.describe(plan, dry_run=dry_run))
    return any(plan.conflicts for plan in plans)


def install_plans(ctx: Context, specs: dict[str, InstallSpec], *, force: bool) -> list[install.Plan]:
    """Plan the kit install and every runtime install.

    Args:
        ctx: The command context.
        specs: Runtime names mapped to their specs.
        force: Whether to replace user-edited files.

    Returns:
        The kit plan first, then one plan per runtime.
    """
    extra = {"version": kit.source_version(ctx.source), **kit.provenance(ctx.source)}
    plans = [install.plan(kit_spec(ctx), ctx.manifest(kit.KIT_RUNTIME), force=force, extra=extra)]
    plans += [install.plan(spec, ctx.manifest(rt), force=force) for rt, spec in specs.items()]
    return plans


def cmd_install(ctx: Context, runtimes: tuple[str, ...], *, dry_run: bool, force: bool) -> int:
    """Install the kit, its venv and the given runtimes.

    Args:
        ctx: The command context.
        runtimes: The runtimes to install.
        dry_run: Whether to only print the plan.
        force: Whether to replace user-edited files, keeping backups.

    Returns:
        EXIT_ISSUES when conflicts were skipped, otherwise EXIT_OK.
    """
    specs = {runtime: runtime_spec(ctx, runtime) for runtime in runtimes}
    plans = install_plans(ctx, specs, force=force)
    conflicts = report(ctx, plans, dry_run=dry_run)
    base = kit.venv_base(ctx.kit, ctx.toolchain)
    if dry_run:
        venv = f"would create {kit.venv_dir(ctx.kit)} from {base}" if base else "kit venv is usable"
        ctx.say(f"[kit] {venv}", "Dry run: nothing was written.")
        return EXIT_ISSUES if conflicts else EXIT_OK
    backups = install.backup_dir(ctx.home)
    install.apply(plans[0], ctx.manifest_file(kit.KIT_RUNTIME), backups)
    python = kit.create_kit_venv(ctx.kit, base, ctx.toolchain) if base else kit.venv_python(ctx.kit)
    for plan in plans[1:]:
        install.apply(plan, ctx.manifest_file(plan.runtime), backups)
    ctx.say(
        f"[kit] hooks run {python}",
        f"Installed {', '.join(runtimes)}. Start a new session to load the hooks; "
        "hook trust is unchanged, review new hooks in each runtime.",
    )
    if conflicts:
        ctx.say("Some files were skipped because of conflicts (see above).")
    return EXIT_ISSUES if conflicts else EXIT_OK


def kit_removal(ctx: Context, *, force: bool) -> install.Plan | None:
    """Plan removal of the kit, its venv and its bytecode caches.

    Args:
        ctx: The command context.
        force: Whether to remove user-edited kit files.

    Returns:
        The plan, or None when the kit is not installed.
    """
    manifest = ctx.manifest(kit.KIT_RUNTIME)
    if not manifest:
        return None
    plan = install.plan_uninstall(InstallSpec(kit.KIT_RUNTIME), manifest, force=force)
    venv = kit.venv_dir(ctx.kit)
    if venv.exists() or venv.is_symlink():
        plan.changes.append(install.Change(install.REMOVE_TREE, venv, reason="venv", backup=False))
    plan.changes += [
        install.Change(install.REMOVE_TREE, cache, reason="bytecode", backup=False)
        for cache in kit.bytecode_dirs(ctx.kit)
    ]
    return plan


def uninstall_plans(ctx: Context, runtimes: tuple[str, ...], *, force: bool) -> list[install.Plan]:
    """Plan the uninstall of the given runtimes, and of the kit once no runtime is left.

    Args:
        ctx: The command context.
        runtimes: The runtimes to uninstall; ones not installed are reported.
        force: Whether to remove user-edited files.

    Returns:
        One plan per installed runtime, then the kit plan when the kit goes too.
    """
    plans = []
    for runtime in runtimes:
        manifest = ctx.manifest(runtime)
        if manifest:
            plans.append(install.plan_uninstall(runtime_spec(ctx, runtime), manifest, force=force))
        else:
            ctx.say(f"[{runtime}] not installed")
    removed = {plan.runtime for plan in plans if plan.manifest is None}
    remaining = [rt for rt in RUNTIMES if rt not in removed and ctx.manifest(rt)]
    kit_plan = None if remaining else kit_removal(ctx, force=force)
    return plans + ([kit_plan] if kit_plan else [])


def cmd_uninstall(ctx: Context, runtimes: tuple[str, ...], *, dry_run: bool, force: bool) -> int:
    """Uninstall the given runtimes, and the kit once no runtime is left.

    Args:
        ctx: The command context.
        runtimes: The runtimes to uninstall.
        dry_run: Whether to only print the plan.
        force: Whether to remove user-edited files, keeping backups.

    Returns:
        EXIT_ISSUES when edited files were kept, otherwise EXIT_OK.
    """
    plans = uninstall_plans(ctx, runtimes, force=force)
    conflicts = report(ctx, plans, dry_run=dry_run)
    if dry_run:
        ctx.say("Dry run: nothing was changed.")
        return EXIT_ISSUES if conflicts else EXIT_OK
    backups = install.backup_dir(ctx.home)
    for plan in plans:
        install.apply(plan, ctx.manifest_file(plan.runtime), backups)
    if conflicts:
        ctx.say("Edited files were kept (see above); rerun with --force to remove them.")
    return EXIT_ISSUES if conflicts else EXIT_OK


def kit_problems(ctx: Context) -> list[str]:
    """Check the kit files, its interpreter and whether its source moved on.

    Args:
        ctx: The command context.

    Returns:
        One message per problem.
    """
    manifest = ctx.manifest(kit.KIT_RUNTIME)
    python = kit.venv_python(ctx.kit)
    problems = install.doctor(kit_spec(ctx), manifest, ctx.kit, python)
    if manifest:
        found = [kit.interpreter_problem(python, ctx.toolchain), stale_kit_problem(ctx, manifest)]
        problems += [problem for problem in found if problem]
    return problems


def stale_kit_problem(ctx: Context, manifest: dict) -> str | None:
    """Report a kit whose source checkout has a different HEAD.

    Args:
        ctx: The command context.
        manifest: The kit manifest.

    Returns:
        The problem with its update command, or None when the kit is current.
    """
    found = kit.behind(manifest)
    if found is None:
        return None
    source, recorded, current = found
    return (
        f"installed from {source} at {recorded[:8]}, but the repo is now at {current[:8]}; "
        f"update with: {kit.update_command(ctx.home, source)}"
    )


def cmd_doctor(ctx: Context, runtimes: tuple[str, ...]) -> int:
    """Report drift between the rendered install and the home for the kit and the given runtimes.

    Args:
        ctx: The command context.
        runtimes: The runtimes to check.

    Returns:
        EXIT_ISSUES when none is installed or any problem was found, otherwise EXIT_OK.
    """
    found = {kit.KIT_RUNTIME: kit_problems(ctx)}
    installed = [rt for rt in runtimes if ctx.manifest(rt)]
    for runtime in runtimes:
        if runtime not in installed:
            ctx.say(f"[{runtime}] not installed")
            continue
        found[runtime] = install.doctor(
            runtime_spec(ctx, runtime), ctx.manifest(runtime), ctx.kit, kit.venv_python(ctx.kit)
        )
    for runtime, problems in found.items():
        ctx.say(*([f"[{runtime}] {problem}" for problem in problems] or [f"[{runtime}] ok"]))
    return EXIT_ISSUES if not installed or any(found.values()) else EXIT_OK


def event_summary(ctx: Context, runtime: str) -> str:
    """Summarize a runtime's logged events.

    Args:
        ctx: The command context.
        runtime: The runtime name.

    Returns:
        The event count and the last event with its UTC time.
    """
    path = config.state_dir(runtime_config(ctx, runtime), ctx.home, runtime) / events.EVENTS_FILE
    entries = events.read(path)
    if not entries:
        return "0 events"
    last = entries[-1]
    when = last.get("ts")
    stamp = (
        dt.datetime.fromtimestamp(when, dt.UTC).isoformat(timespec="seconds")
        if isinstance(when, (int, float))
        else "unknown time"
    )
    return f"{len(entries)} events (last: {last.get('event', '?')} at {stamp})"


def runtime_status(ctx: Context, runtime: str) -> str:
    """Summarize one runtime's install and events.

    Args:
        ctx: The command context.
        runtime: The runtime name.

    Returns:
        One status line.
    """
    manifest = ctx.manifest(runtime)
    if not manifest:
        return f"[{runtime}] not installed"
    counts = ", ".join(f"{len(manifest.get(section, {}))} {section}" for section in install.SECTIONS)
    return f"[{runtime}] installed {manifest.get('installed_at', '?')}: {counts}; {event_summary(ctx, runtime)}"


def kit_source_lines(manifest: dict) -> list[str]:
    """Describe the kit's source checkout and whether it moved on.

    Args:
        manifest: The kit manifest.

    Returns:
        One line, or none when no source is recorded.
    """
    source, commit = manifest.get("source"), manifest.get("commit")
    if not source:
        return []
    line = f"[kit] source {source}, commit {commit[:8] if commit else 'unknown'}"
    found = kit.behind(manifest)
    if found is not None:
        line += f"; behind: repo at {found[2][:8]}"
    return [line]


def cmd_status(ctx: Context, runtimes: tuple[str, ...]) -> int:
    """Print the kit's and the given runtimes' install status.

    Args:
        ctx: The command context.
        runtimes: The runtimes to report.

    Returns:
        EXIT_OK.
    """
    manifest = ctx.manifest(kit.KIT_RUNTIME)
    if manifest:
        version = ctx.toolchain.version(kit.venv_python(ctx.kit))
        python = f"{version[0]}.{version[1]}" if version else "not runnable"
        ctx.say(
            f"[kit] version {manifest.get('version', 'unknown')}, hash {install.manifest_hash(manifest)}, "
            f"{len(manifest['files'])} files, python {python}, installed {manifest.get('installed_at', '?')}"
        )
        ctx.say(*kit_source_lines(manifest))
    else:
        ctx.say("[kit] not installed")
    ctx.say(*(runtime_status(ctx, runtime) for runtime in runtimes))
    return EXIT_OK


def parse_setting(text: str) -> tuple[str, str, object]:
    """Parse one `--set section.key=value` argument.

    Args:
        text: The argument.

    Returns:
        (section, key, value); the value is parsed as JSON, falling back to the raw string.

    Raises:
        CliError: The argument is not `section.key=value` with a single-level key.
    """
    name, sep, raw = text.partition("=")
    section, dot, key = name.strip().partition(".")
    if not sep or not dot or not section or not key or "." in key:
        msg = f"--set expects section.key=value, got {text!r}"
        raise CliError(msg)
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    return section, key, value


def settings_schema(ctx: Context) -> dict:
    """Collect every settable section: the shipped defaults plus each adapter's config.

    Args:
        ctx: The command context.

    Returns:
        Sections mapped to their known keys and default values.
    """
    schema = read_json(config.DEFAULTS_FILE)
    for runtime in RUNTIMES:
        config.deep_merge(schema, read_json(ctx.source / "adapters" / runtime / "config.json"))
    return schema


def check_setting(schema: dict, section: str, key: str, value: object) -> None:
    """Validate one setting against the schema.

    Args:
        schema: The settings schema.
        section: The setting's section.
        key: The setting's key; new keys are allowed only in sections of objects.
        value: The parsed value.

    Raises:
        CliError: The section or key is unknown, or the value has the wrong type.
    """
    known = schema.get(section)
    if not isinstance(known, dict):
        msg = f"unknown setting section {section!r}"
        raise CliError(msg)
    if key not in known and not (config.holds_objects(known) and isinstance(value, dict)):
        msg = f"unknown setting {section}.{key}; known: {', '.join(sorted(known))}"
        raise CliError(msg)
    if key in known and not config.valid_override(value, known[key]):
        msg = f"{section}.{key} expects {type(known[key]).__name__}, got {value!r}"
        raise CliError(msg)


def setting_updates(ctx: Context, settings: list[str]) -> dict:
    """Parse and validate every `--set` argument.

    Args:
        ctx: The command context.
        settings: The raw arguments.

    Returns:
        Sections mapped to their updated keys and values.
    """
    schema = settings_schema(ctx)
    updates: dict = {}
    for section, key, value in map(parse_setting, settings):
        check_setting(schema, section, key, value)
        updates.setdefault(section, {})[key] = value
    return updates


def apply_settings(ctx: Context, settings: list[str], *, dry_run: bool) -> None:
    """Write `--set` arguments to the user config.

    Args:
        ctx: The command context.
        settings: The raw arguments.
        dry_run: Whether to only print the update.

    Raises:
        CliError: The user config cannot be updated.
    """
    updates = setting_updates(ctx, settings)
    path = config.user_config_file(ctx.home)
    if dry_run:
        ctx.say(f"would update {path}: {json.dumps(updates)}")
        return
    try:
        config.update_user_file(path, updates)
    except (TypeError, ValueError) as exc:
        msg = f"cannot update {path}: {exc}"
        raise CliError(msg) from exc
    ctx.say(f"updated {path}")


def package_config(ctx: Context, runtime: str, overrides: dict) -> dict:
    """Build a package's config: shipped defaults, adapter config and baked overrides.

    The packager's own user config stays out.

    Args:
        ctx: The command context.
        runtime: The runtime name.
        overrides: Settings to bake in.

    Returns:
        The merged config.
    """
    merged = config.deep_merge(
        read_json(ctx.source / "content" / "defaults.json"),
        read_json(ctx.source / "adapters" / runtime / "config.json"),
    )
    return config.deep_merge(merged, overrides)


def package_runtime(
    ctx: Context, runtime: str, out: Path, marketplace: str | None, overrides: dict, *, dry_run: bool
) -> None:
    """Render one runtime's plugin into the output directory.

    Args:
        ctx: The command context.
        runtime: The runtime name.
        out: The package output directory.
        marketplace: Name of a marketplace listing to write as well, or None.
        overrides: Settings baked into the package.
        dry_run: Whether to plan and validate without writing.

    Raises:
        CliError: The runtime has no plugin, the package cannot be rendered, or it cannot be written.
    """
    try:
        module = ctx.load_plugin(runtime)
        build, listing_for = module.plugin_spec, module.marketplace
    except (ImportError, AttributeError) as exc:
        msg = f"{runtime} has no plugin package (adapters/{runtime}/plugin.py): {exc}"
        raise CliError(msg) from exc
    try:
        spec = build(package_config(ctx, runtime, overrides))
        files = package.plan(ctx.source, runtime, spec, overrides)
    except ValueError as exc:
        msg = f"{runtime}: cannot render the package: {exc}"
        raise CliError(msg) from exc
    listing = listing_for(marketplace, spec) if marketplace else None
    target = package.plugin_dir(out, spec)
    if dry_run:
        ctx.say(
            f"[{runtime}] would package {spec.name} {spec.manifest.get('version')} ({len(files)} files) at {target}",
            *([f"[{runtime}] would list it in {out / listing[0]}"] if listing else []),
        )
        return
    try:
        written = package.write(out, spec, files, listing)
    except package.PackageError as exc:
        raise CliError(str(exc)) from exc
    ctx.say(
        f"[{runtime}] packaged {spec.name} {spec.manifest.get('version')} ({len(files)} files) at {target}",
        *([f"[{runtime}] marketplace {written.get('name')}: {out / listing[0]}"] if written is not None else []),
    )


def cmd_package(ctx: Context, runtimes: tuple[str, ...], args: argparse.Namespace) -> int:
    """Render each runtime's plugin into `--out`, baking in `--set` values.

    Args:
        ctx: The command context.
        runtimes: The runtimes to package.
        args: The parsed arguments, for out, marketplace, settings and dry_run.

    Returns:
        EXIT_OK.

    Raises:
        CliError: `--out` is missing.
    """
    if args.out is None:
        msg = "package needs --out DIR"
        raise CliError(msg)
    overrides = setting_updates(ctx, args.settings)
    out = args.out.expanduser().absolute()
    for runtime in runtimes:
        package_runtime(ctx, runtime, out, args.marketplace, overrides, dry_run=args.dry_run)
    if args.dry_run:
        ctx.say("Dry run: nothing was written.")
    return EXIT_OK


def dispatch(ctx: Context, runtimes: tuple[str, ...], args: argparse.Namespace) -> int:
    """Run the requested command; every command but package applies `--set` to the user config first.

    Args:
        ctx: The command context.
        runtimes: The selected runtimes.
        args: The parsed arguments.

    Returns:
        The command's exit code.
    """
    if args.command == "package":
        return cmd_package(ctx, runtimes, args)
    if args.settings:
        apply_settings(ctx, args.settings, dry_run=args.dry_run)
    if args.command == "install":
        return cmd_install(ctx, runtimes, dry_run=args.dry_run, force=args.force)
    if args.command == "uninstall":
        return cmd_uninstall(ctx, runtimes, dry_run=args.dry_run, force=args.force)
    if args.command == "doctor":
        return cmd_doctor(ctx, runtimes)
    return cmd_status(ctx, runtimes)


def parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The parser.
    """
    result = argparse.ArgumentParser(prog="context-guard", description=__doc__.splitlines()[0])
    result.add_argument("command", choices=COMMANDS)
    result.add_argument(
        "--runtime",
        required=True,
        action="append",
        choices=(*RUNTIMES, "all"),
        help="repeatable; all selects every runtime",
    )
    result.add_argument("--home", type=Path, default=Path.home(), help="home directory to install into")
    result.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    result.add_argument("--force", action="store_true", help="replace conflicting files, keeping backups")
    result.add_argument(
        "--set",
        dest="settings",
        action="append",
        default=[],
        metavar="SECTION.KEY=VALUE",
        help="write a setting to <home>/.context-guard/config.json (repeatable); "
        "with package, bake it into the package instead",
    )
    result.add_argument("--out", type=Path, help="package: directory to render the plugin into")
    result.add_argument("--marketplace", metavar="NAME", help="package: also write a marketplace listing")
    return result


def main(
    argv: list[str] | None = None,
    *,
    toolchain: kit.Toolchain | None = None,
    load_mapping: Callable[[str], ModuleType] = import_mapping,
    source: Path = ROOT,
    out: TextIO | None = None,
) -> int:
    """Run the CLI; expected failures print one stderr line and exit with EXIT_ERROR.

    Args:
        argv: The arguments without the program name, or None for `sys.argv`.
        toolchain: Interpreter discovery and venv creation, or None for the real toolchain.
        load_mapping: Import a runtime's adapter mapping.
        source: The source checkout to install or package from.
        out: Where user-facing lines are printed, or None for stdout.

    Returns:
        EXIT_OK, EXIT_ISSUES or EXIT_ERROR.
    """
    args = parser().parse_args(argv)
    ctx = Context(
        args.home.expanduser().absolute(), source, toolchain or kit.Toolchain(), load_mapping, out or sys.stdout
    )
    runtimes = RUNTIMES if "all" in args.runtime else tuple(dict.fromkeys(args.runtime))
    try:
        return dispatch(ctx, runtimes, args)
    except (CliError, kit.ToolchainError, OSError) as exc:
        print(f"context-guard: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
