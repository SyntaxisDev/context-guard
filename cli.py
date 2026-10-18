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
    return importlib.import_module(f"adapters.{runtime}.mapping")


def import_plugin(runtime: str) -> ModuleType:
    return importlib.import_module(f"adapters.{runtime}.plugin")


@dataclass(frozen=True)
class Context:
    home: Path
    source: Path
    toolchain: kit.Toolchain
    load_mapping: Callable[[str], ModuleType]
    out: TextIO
    load_plugin: Callable[[str], ModuleType] = import_plugin

    @property
    def kit(self) -> Path:
        return kit.kit_dir(self.home)

    def say(self, *lines: str) -> None:
        for line in lines:
            print(line, file=self.out)

    def manifest_file(self, runtime: str) -> Path:
        return install.manifest_path(self.home, runtime)

    def manifest(self, runtime: str) -> dict:
        return install.read_manifest(self.manifest_file(runtime))


def runtime_sources(ctx: Context, runtime: str) -> config.ConfigSources:
    adapter = ctx.source / "adapters" / runtime / "config.json"
    return config.sources_for(runtime, ctx.home, adapter if adapter.is_file() else None)


def runtime_config(ctx: Context, runtime: str) -> dict:
    return config.load_config(runtime_sources(ctx, runtime))


def runtime_spec(ctx: Context, runtime: str) -> InstallSpec:
    try:
        mapping = ctx.load_mapping(runtime)
        build = mapping.install_spec
    except (ImportError, AttributeError) as exc:
        raise CliError(f"adapters/{runtime}/mapping.py is unavailable: {exc}") from exc
    try:
        spec = build(ctx.home, ctx.kit, kit.venv_python(ctx.kit), runtime_config(ctx, runtime))
    except ValueError as exc:
        raise CliError(f"{runtime}: cannot render the install: {exc}") from exc
    if spec.runtime != runtime:
        raise CliError(f"adapters/{runtime}/mapping.py returned a spec for {spec.runtime!r}")
    return spec


def kit_spec(ctx: Context) -> InstallSpec:
    return kit.kit_spec(ctx.kit, ctx.source)


def report(ctx: Context, plans: list[install.Plan], dry_run: bool) -> bool:
    """Print every plan; return whether any conflict was skipped."""
    for plan in plans:
        ctx.say(*install.describe(plan, dry_run))
    return any(plan.conflicts for plan in plans)


def install_plans(ctx: Context, specs: dict[str, InstallSpec], force: bool) -> list[install.Plan]:
    extra = {"version": kit.source_version(ctx.source), **kit.provenance(ctx.source)}
    plans = [install.plan(kit_spec(ctx), ctx.manifest(kit.KIT_RUNTIME), force=force, extra=extra)]
    plans += [install.plan(spec, ctx.manifest(rt), force=force) for rt, spec in specs.items()]
    return plans


def cmd_install(ctx: Context, runtimes: tuple[str, ...], dry_run: bool, force: bool) -> int:
    specs = {runtime: runtime_spec(ctx, runtime) for runtime in runtimes}
    plans = install_plans(ctx, specs, force)
    conflicts = report(ctx, plans, dry_run)
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
    ctx.say(f"[kit] hooks run {python}",
            f"Installed {', '.join(runtimes)}. Start a new session to load the hooks; "
            "hook trust is unchanged, review new hooks in each runtime.")
    if conflicts:
        ctx.say("Some files were skipped because of conflicts (see above).")
    return EXIT_ISSUES if conflicts else EXIT_OK


def kit_removal(ctx: Context, force: bool) -> install.Plan | None:
    manifest = ctx.manifest(kit.KIT_RUNTIME)
    if not manifest:
        return None
    plan = install.plan_uninstall(InstallSpec(kit.KIT_RUNTIME), manifest, force=force)
    venv = kit.venv_dir(ctx.kit)
    if venv.exists() or venv.is_symlink():
        plan.changes.append(install.Change(install.REMOVE_TREE, venv, reason="venv", backup=False))
    plan.changes += [install.Change(install.REMOVE_TREE, cache, reason="bytecode", backup=False)
                     for cache in kit.bytecode_dirs(ctx.kit)]
    return plan


def uninstall_plans(ctx: Context, runtimes: tuple[str, ...], force: bool) -> list[install.Plan]:
    plans = []
    for runtime in runtimes:
        manifest = ctx.manifest(runtime)
        if manifest:
            plans.append(install.plan_uninstall(runtime_spec(ctx, runtime), manifest, force=force))
        else:
            ctx.say(f"[{runtime}] not installed")
    removed = {plan.runtime for plan in plans if plan.manifest is None}
    remaining = [rt for rt in RUNTIMES if rt not in removed and ctx.manifest(rt)]
    kit_plan = None if remaining else kit_removal(ctx, force)
    return plans + ([kit_plan] if kit_plan else [])


def cmd_uninstall(ctx: Context, runtimes: tuple[str, ...], dry_run: bool, force: bool) -> int:
    plans = uninstall_plans(ctx, runtimes, force)
    conflicts = report(ctx, plans, dry_run)
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
    manifest = ctx.manifest(kit.KIT_RUNTIME)
    python = kit.venv_python(ctx.kit)
    problems = install.doctor(kit_spec(ctx), manifest, ctx.kit, python)
    if manifest:
        found = [kit.interpreter_problem(python, ctx.toolchain), kit.staleness(manifest)]
        problems += [problem for problem in found if problem]
    return problems


def cmd_doctor(ctx: Context, runtimes: tuple[str, ...], dry_run: bool, force: bool) -> int:
    found = {kit.KIT_RUNTIME: kit_problems(ctx)}
    installed = [rt for rt in runtimes if ctx.manifest(rt)]
    for runtime in runtimes:
        if runtime not in installed:
            ctx.say(f"[{runtime}] not installed")
            continue
        found[runtime] = install.doctor(runtime_spec(ctx, runtime), ctx.manifest(runtime),
                                        ctx.kit, kit.venv_python(ctx.kit))
    for runtime, problems in found.items():
        ctx.say(*([f"[{runtime}] {problem}" for problem in problems] or [f"[{runtime}] ok"]))
    return EXIT_ISSUES if not installed or any(found.values()) else EXIT_OK


def event_summary(ctx: Context, runtime: str) -> str:
    path = config.state_dir(runtime_config(ctx, runtime), ctx.home, runtime) / events.EVENTS_FILE
    entries = events.read(path)
    if not entries:
        return "0 events"
    last = entries[-1]
    when = last.get("ts")
    stamp = (dt.datetime.fromtimestamp(when, dt.UTC).isoformat(timespec="seconds")
             if isinstance(when, (int, float)) else "unknown time")
    return f"{len(entries)} events (last: {last.get('event', '?')} at {stamp})"


def runtime_status(ctx: Context, runtime: str) -> str:
    manifest = ctx.manifest(runtime)
    if not manifest:
        return f"[{runtime}] not installed"
    counts = ", ".join(f"{len(manifest.get(section, {}))} {section}" for section in install.SECTIONS)
    return (f"[{runtime}] installed {manifest.get('installed_at', '?')}: {counts}; "
            f"{event_summary(ctx, runtime)}")


def kit_source_lines(manifest: dict) -> list[str]:
    source, commit = manifest.get("source"), manifest.get("commit")
    if not source:
        return []
    line = f"[kit] source {source}, commit {commit[:8] if commit else 'unknown'}"
    if kit.staleness(manifest):
        line += f"; behind: repo at {kit.source_commit(Path(source))[:8]}"
    return [line]


def cmd_status(ctx: Context, runtimes: tuple[str, ...], dry_run: bool, force: bool) -> int:
    manifest = ctx.manifest(kit.KIT_RUNTIME)
    if manifest:
        version = ctx.toolchain.version(kit.venv_python(ctx.kit))
        python = f"{version[0]}.{version[1]}" if version else "not runnable"
        ctx.say(f"[kit] version {manifest.get('version', 'unknown')}, hash {install.manifest_hash(manifest)}, "
                f"{len(manifest['files'])} files, python {python}, installed {manifest.get('installed_at', '?')}")
        ctx.say(*kit_source_lines(manifest))
    else:
        ctx.say("[kit] not installed")
    ctx.say(*(runtime_status(ctx, runtime) for runtime in runtimes))
    return EXIT_OK


def parse_setting(text: str) -> tuple[str, str, object]:
    name, sep, raw = text.partition("=")
    section, dot, key = name.strip().partition(".")
    if not sep or not dot or not section or not key or "." in key:
        raise CliError(f"--set expects section.key=value, got {text!r}")
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    return section, key, value


def setting_updates(settings: list[str]) -> dict:
    defaults = read_json(config.DEFAULTS_FILE)
    updates: dict = {}
    for section, key, value in map(parse_setting, settings):
        known = defaults.get(section) if isinstance(defaults.get(section), dict) else {}
        if key in known and not config.valid_override(value, known[key]):
            raise CliError(f"{section}.{key} expects {type(known[key]).__name__}, got {value!r}")
        updates.setdefault(section, {})[key] = value
    return updates


def apply_settings(ctx: Context, settings: list[str], dry_run: bool) -> None:
    updates = setting_updates(settings)
    loader = config.ConfigLoader(config.sources_for(RUNTIMES[0], ctx.home, None))
    if dry_run:
        ctx.say(f"would update {loader.user_config_path()}: {json.dumps(updates)}")
        return
    try:
        path = loader.update_user_config(updates)
    except (TypeError, ValueError) as exc:
        raise CliError(f"cannot update {loader.user_config_path()}: {exc}") from exc
    ctx.say(f"updated {path}")


def package_config(ctx: Context, runtime: str, overrides: dict) -> dict:
    """Shipped defaults, adapter config and baked overrides; the packager's own user config stays out."""
    merged = config.deep_merge(read_json(ctx.source / "content" / "defaults.json"),
                               read_json(ctx.source / "adapters" / runtime / "config.json"))
    return config.deep_merge(merged, overrides)


def package_runtime(ctx: Context, runtime: str, out: Path, marketplace: str | None,
                    overrides: dict, dry_run: bool) -> None:
    try:
        module = ctx.load_plugin(runtime)
        build, listing_for = module.plugin_spec, module.marketplace
    except (ImportError, AttributeError) as exc:
        raise CliError(f"{runtime} has no plugin package (adapters/{runtime}/plugin.py): {exc}") from exc
    try:
        spec = build(package_config(ctx, runtime, overrides))
        files = package.plan(ctx.source, runtime, spec, overrides)
    except ValueError as exc:
        raise CliError(f"{runtime}: cannot render the package: {exc}") from exc
    listing = listing_for(marketplace, spec) if marketplace else None
    target = package.plugin_dir(out, spec)
    if dry_run:
        ctx.say(f"[{runtime}] would package {spec.name} {spec.manifest.get('version')} "
                f"({len(files)} files) at {target}", *([f"[{runtime}] would list it in {out / listing[0]}"]
                                                      if listing else []))
        return
    try:
        package.write(out, spec, files, listing)
    except package.PackageError as exc:
        raise CliError(str(exc)) from exc
    ctx.say(f"[{runtime}] packaged {spec.name} {spec.manifest.get('version')} ({len(files)} files) at {target}",
            *([f"[{runtime}] marketplace {marketplace}: {out / listing[0]}"] if listing else []))


def cmd_package(ctx: Context, runtimes: tuple[str, ...], args: argparse.Namespace) -> int:
    if args.out is None:
        raise CliError("package needs --out DIR")
    overrides = setting_updates(args.settings)
    out = args.out.expanduser().absolute()
    for runtime in runtimes:
        package_runtime(ctx, runtime, out, args.marketplace, overrides, args.dry_run)
    if args.dry_run:
        ctx.say("Dry run: nothing was written.")
    return EXIT_OK


HANDLERS = {"install": cmd_install, "uninstall": cmd_uninstall, "doctor": cmd_doctor, "status": cmd_status}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="context-guard", description=__doc__.splitlines()[0])
    result.add_argument("command", choices=COMMANDS)
    result.add_argument("--runtime", required=True, action="append", choices=RUNTIMES + ("all",),
                        help="repeatable; all selects every runtime")
    result.add_argument("--home", type=Path, default=Path.home(), help="home directory to install into")
    result.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    result.add_argument("--force", action="store_true", help="replace conflicting files, keeping backups")
    result.add_argument("--set", dest="settings", action="append", default=[], metavar="SECTION.KEY=VALUE",
                        help="write a setting to <home>/.context-guard/config.json (repeatable); "
                             "with package, bake it into the package instead")
    result.add_argument("--out", type=Path, help="package: directory to render the plugin into")
    result.add_argument("--marketplace", metavar="NAME", help="package: also write a marketplace listing")
    return result


def main(argv: list[str] | None = None, *, toolchain: kit.Toolchain | None = None,
         load_mapping: Callable[[str], ModuleType] = import_mapping, source: Path = ROOT,
         out: TextIO | None = None) -> int:
    args = parser().parse_args(argv)
    ctx = Context(args.home.expanduser().absolute(), source, toolchain or kit.Toolchain(),
                  load_mapping, out or sys.stdout)
    runtimes = RUNTIMES if "all" in args.runtime else tuple(dict.fromkeys(args.runtime))
    try:
        if args.command == "package":
            return cmd_package(ctx, runtimes, args)
        if args.settings:
            apply_settings(ctx, args.settings, args.dry_run)
        return HANDLERS[args.command](ctx, runtimes, args.dry_run, args.force)
    except (CliError, kit.ToolchainError, OSError) as exc:
        print(f"context-guard: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
