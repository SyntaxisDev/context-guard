"""One configuration precedence path for every runtime.

content/defaults.json, then the adapter's config.json, then the user's
~/.context-guard/config.json (top level, then its runtimes.<runtime> section),
then CONTEXT_GUARD_<SECTION>_<KEY> environment variables. User and environment
values must match the type of the shipped value they replace; malformed values
are ignored. Unknown keys are kept, except that a new entry in a nested section of
objects (tier profiles) must be an object too.
"""

import copy
import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from core.state import file_lock, read_json, write_json

DEFAULTS_FILE = Path(__file__).resolve().parent.parent / "content" / "defaults.json"
KIT_DIRNAME = ".context-guard"
KIT_NAME = "kit"
HOME_ENV = "CONTEXT_GUARD_HOME"
RUNTIMES_KEY = "runtimes"
TRUE_WORDS = {"1", "true", "yes", "on"}
FALSE_WORDS = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class ConfigSources:
    """The files one runtime's configuration is merged from, lowest precedence first.

    Attributes:
        runtime: Runtime name selecting the user file's `runtimes.<runtime>` section.
        defaults: Shipped defaults file.
        adapter: The adapter's config.json, or None when the runtime ships none.
        user: The user's config.json; it may be missing.
    """

    runtime: str
    defaults: Path
    adapter: Path | None
    user: Path


def kit_root(home: Path) -> Path:
    """Return the context-guard directory inside a home.

    Args:
        home: Home directory context-guard is installed into.

    Returns:
        `<home>/.context-guard`.
    """
    return home / KIT_DIRNAME


def installed_kit(kit: Path) -> bool:
    """Tell whether a kit directory is an installed kit rather than a source checkout.

    Args:
        kit: Directory the running hook code lives in.

    Returns:
        True when `kit` is `<home>/.context-guard/kit`.
    """
    return kit.name == KIT_NAME and kit.parent.name == KIT_DIRNAME


def home_override(environ: Mapping[str, str] | None) -> Path | None:
    """Return the home named by CONTEXT_GUARD_HOME, if it is set to valid text.

    Args:
        environ: Environment to read; None reads the process environment.

    Returns:
        The expanded override path, or None when it is unset, empty or not valid text.
    """
    override = (os.environ if environ is None else environ).get(HOME_ENV, "")
    return Path(override).expanduser() if override and valid_text(override) else None


def resolve_home(kit: Path, environ: Mapping[str, str] | None = None) -> Path:
    """Return the home a hook or command acts on.

    CONTEXT_GUARD_HOME wins, then the home an installed kit (<home>/.context-guard/kit) lives in,
    then the user's home, so a kit installed with --home never writes state into another home.

    Args:
        kit: Directory the running hook code lives in.
        environ: Environment to read; None reads the process environment.

    Returns:
        The resolved home directory.
    """
    override = home_override(environ)
    if override is not None:
        return override
    if installed_kit(kit):
        return kit.parent.parent
    return Path.home()


def foreign_home(kit: Path, environ: Mapping[str, str] | None = None) -> bool:
    """Tell whether an installed kit must stand aside because CONTEXT_GUARD_HOME names another home.

    Every kit registered with the runtime fires, and only the kit installed in that home may act there.

    Args:
        kit: Directory the running hook code lives in.
        environ: Environment to read; None reads the process environment.

    Returns:
        True when `kit` is installed and the override resolves to a different home.
    """
    override = home_override(environ)
    if override is None or not installed_kit(kit):
        return False
    return override.resolve() != kit.parent.parent.resolve()


def user_config_file(home: Path) -> Path:
    """Return the user's configuration file in a home.

    Args:
        home: Home directory context-guard is installed into.

    Returns:
        `<home>/.context-guard/config.json`.
    """
    return kit_root(home) / "config.json"


def sources_for(runtime: str, home: Path, adapter: Path | None) -> ConfigSources:
    """Build the configuration sources for one runtime in one home.

    Args:
        runtime: Runtime name.
        home: Home directory holding the user file.
        adapter: The adapter's config.json, or None.

    Returns:
        The shipped defaults, the adapter file and the user file for `runtime`.
    """
    return ConfigSources(runtime, DEFAULTS_FILE, adapter, user_config_file(home))


def expand_path(value: str, home: Path) -> Path:
    """Expand a leading ~ against an explicit home rather than the process environment.

    Args:
        value: Path text, possibly starting with `~` or `~/`.
        home: Home directory that `~` stands for.

    Returns:
        The path with `~` replaced by `home`; other paths are returned unchanged.
    """
    if value == "~" or value.startswith("~/"):
        return home / value[2:]
    return Path(value)


def handoff_dir(config: dict, home: Path) -> Path:
    """Return the configured handoff store.

    Args:
        config: Loaded configuration with `paths.handoff_dir`.
        home: Home directory that `~` stands for.

    Returns:
        The expanded handoff directory.
    """
    return expand_path(config["paths"]["handoff_dir"], home)


def state_dir(config: dict, home: Path, runtime: str) -> Path:
    """Return one runtime's session state directory.

    Args:
        config: Loaded configuration with `paths.state_dir`.
        home: Home directory that `~` stands for.
        runtime: Runtime name; each runtime gets its own subdirectory.

    Returns:
        The expanded state directory for `runtime`.
    """
    return expand_path(config["paths"]["state_dir"], home) / runtime


def deep_merge(base: dict, extra: Mapping) -> dict:
    """Merge `extra` into `base` in place, recursing where both sides hold objects.

    Args:
        base: Mapping to update; nested dicts are updated in place.
        extra: Values to copy over `base`; they are deep-copied.

    Returns:
        `base`, updated.
    """
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def valid_text(value: object) -> bool:
    """Tell whether a value is a string that can be stored and passed on safely.

    Args:
        value: Candidate value.

    Returns:
        True for a string without NUL characters that encodes as UTF-8.
    """
    if not isinstance(value, str) or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def valid_override(value: object, default: object) -> bool:
    """Tell whether a user or environment value may replace a shipped value.

    Args:
        value: Candidate override.
        default: The shipped value it would replace; its type sets the expected type. Lists of one
            item type require every override item to be valid for the first default item.

    Returns:
        True when `value` has the shipped value's type and is well formed (finite floats, valid text).
    """
    if isinstance(default, dict):
        return isinstance(value, dict)
    if isinstance(default, bool):
        return type(value) is bool
    if isinstance(default, int):
        return type(value) is int
    if isinstance(default, float):
        try:
            return type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            return False
    if isinstance(default, str):
        return valid_text(value)
    if isinstance(default, list):
        if not isinstance(value, list):
            return False
        if default and all(type(item) is type(default[0]) for item in default):
            return all(valid_override(item, default[0]) for item in value)
        return True
    return type(value) is type(default)


def holds_objects(section: dict) -> bool:
    """Tell whether a section is a nested section of objects only (tier profiles).

    Args:
        section: Configuration section.

    Returns:
        True when `section` is non-empty and every value is a dict; new entries must then be objects too.
    """
    return bool(section) and all(isinstance(value, dict) for value in section.values())


def merge_user_overrides(base: dict, extra: Mapping, *, nested: bool = False) -> None:
    """Merge user values into `base` in place, keeping only valid overrides of shipped values.

    Unknown keys are added, except that a new entry in a nested section of objects must be an object.

    Args:
        base: Configuration to update.
        extra: User values.
        nested: True when `base` is a section inside the configuration rather than its top level.
    """
    for key, value in extra.items():
        if key not in base:
            if not (nested and holds_objects(base)) or isinstance(value, Mapping):
                base[key] = copy.deepcopy(value)
        elif valid_override(value, base[key]):
            if isinstance(base[key], dict):
                merge_user_overrides(base[key], value, nested=True)
            else:
                base[key] = copy.deepcopy(value)


def parse_env_value(raw: str, default: object) -> object:
    """Parse an environment variable as the type of the shipped value it overrides.

    Args:
        raw: Environment variable text.
        default: The shipped value; booleans accept TRUE_WORDS and FALSE_WORDS, lists are comma-separated.

    Returns:
        The parsed value; strings and other types are returned as `raw`.

    Raises:
        ValueError: When `raw` is not a boolean word, int or float as `default` requires.
    """
    if isinstance(default, bool):
        lowered = raw.strip().lower()
        if lowered in TRUE_WORDS:
            return True
        if lowered in FALSE_WORDS:
            return False
        raise ValueError(raw)
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, list):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def apply_env(config: dict, schema: dict, environ: Mapping[str, str]) -> None:
    """Apply CONTEXT_GUARD_<SECTION>_<KEY> overrides of scalar and list settings in place.

    Unparsable or invalid values are ignored.

    Args:
        config: Configuration to update.
        schema: Shipped configuration whose values set each key's type.
        environ: Environment to read.
    """
    for section, values in config.items():
        if not isinstance(values, dict) or not isinstance(schema.get(section), dict):
            continue
        for key, default in schema[section].items():
            raw = environ.get(f"CONTEXT_GUARD_{section}_{key}".upper())
            if raw is None or isinstance(default, dict):
                continue
            try:
                value = parse_env_value(raw, default)
            except ValueError:
                continue
            if valid_override(value, default):
                values[key] = value


def load_config(sources: ConfigSources, environ: Mapping[str, str] | None = None) -> dict:
    """Load one runtime's configuration in precedence order.

    Missing or malformed files count as empty.

    Args:
        sources: Files to merge.
        environ: Environment to read; None reads the process environment.

    Returns:
        The merged configuration.
    """
    config = read_json(sources.defaults)
    if sources.adapter is not None:
        deep_merge(config, read_json(sources.adapter))
    schema = copy.deepcopy(config)
    user = read_json(sources.user)
    runtimes = user.pop(RUNTIMES_KEY, None)
    merge_user_overrides(config, user)
    section = runtimes.get(sources.runtime) if isinstance(runtimes, dict) else None
    if isinstance(section, dict):
        merge_user_overrides(config, section)
    apply_env(config, schema, os.environ if environ is None else environ)
    return config


class ConfigLoader:
    """Own one runtime's sources, its cached configuration and user-file updates.

    Args:
        sources: Files to merge.
        environ: Environment to read; None reads the process environment at each load.
    """

    def __init__(self, sources: ConfigSources, environ: Mapping[str, str] | None = None) -> None:
        self.sources = sources
        self.environ = environ
        self._cache: dict | None = None

    def user_config_path(self) -> Path:
        """Return the user configuration file.

        Returns:
            The user file from the sources.
        """
        return self.sources.user

    def load(self, *, refresh: bool = False) -> dict:
        """Return the configuration, loading it on first use.

        Args:
            refresh: Reload files and environment instead of using the cache.

        Returns:
            A defensive copy of the cached configuration.
        """
        if self._cache is None or refresh:
            self._cache = load_config(self.sources, self.environ)
        return copy.deepcopy(self._cache)

    def update_user_config(self, updates: dict) -> Path:
        """Merge updates into the user file, then reload the configuration.

        Args:
            updates: Values to merge into the user file.

        Returns:
            The user file written.
        """
        path = update_user_file(self.user_config_path(), updates)
        self.load(refresh=True)
        return path


def update_user_file(path: Path, updates: dict) -> Path:
    """Strictly read the user file, merge the updates and write it atomically under its lock.

    Args:
        path: User configuration file; a missing file starts empty and invalid JSON propagates its ValueError.
        updates: Values to merge into it.

    Returns:
        `path`, written.

    Raises:
        TypeError: When the file holds JSON that is not an object.
    """
    with file_lock(path.with_name(path.name + ".lock")):
        config = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(config, dict):
            msg = f"{path} must contain a JSON object"
            raise TypeError(msg)
        deep_merge(config, updates)
        write_json(path, config)
    return path
