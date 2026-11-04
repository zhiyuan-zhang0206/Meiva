"""A plugin's config face: `default_config.py`, a pure declaration of its config class.

The file holds the plugin's `BaseModel` and exports `contribute()` returning
`PluginContributions(config=Cls, flags=(...))`. Config is optional; flags declare
non-sensitive Core dependencies. Other contribution faces remain separate.
It imports no plugin code or agent runtime, so every reader can load it on its own:

- the plugin loader (`base.packages.plugins.loading`) adds it to the registry and the SDK install binds the class;
- `meiva plugins update` merges each plugin's disk image against the declared class;
- the gateway builds fresh, non-secret cluster policy from these declarations and authority images;
- the gateway and ops validate a per-agent config overlay against the declared classes of the ENABLED
  plugins, in processes that never import `plugin.py`.

`declared_config_class` is the one reader for the last two; a face that fails to load is the plugin's
failure, never the reader's.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
import threading
from pathlib import Path

from pydantic import BaseModel

from base.host.boundary.untyped import object_of
from base.packages.plugins import config_from_image, load_report
from base.packages.plugins.extensions import PluginContributions

CONFIG_FACE = "default_config"

# meiva:note
# Re-entrant: a face that reads another declaration on its own thread must not deadlock itself.
_READING = threading.RLock()


def declared_config_class(name: str, plugin_dir: Path) -> type[BaseModel] | None:
    """The config class `plugin_dir/default_config.py` declares; None when the plugin ships no face.

    Raises:
        Exception: the face fails to load, has no `contribute()`, declares another
            contribution surface, an invalid config class or an invalid Core flag.
    """
    return configuration_declaration(name, plugin_dir).config


def configuration_declaration(name: str, plugin_dir: Path) -> PluginContributions:
    """Read the pure config schema and Core dependencies without installing the plugin.

    A missing face contributes neither. Invalid or sensitive Core dependencies
    fail at declaration admission, independently of an agent SDK installation.
    """
    face_py = plugin_dir / f"{CONFIG_FACE}.py"
    if not face_py.is_file():
        return PluginContributions()
    return _read(name, face_py)


def _read(name: str, face_py: Path) -> PluginContributions:
    dotted = f"plugins.{name}.{CONFIG_FACE}"
    spec = importlib.util.spec_from_file_location(dotted, face_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {face_py}")
    module = importlib.util.module_from_spec(spec)
    # meiva:note
    # One execution of this face at a time. `sys.modules` is where a Pydantic model resolves its
    # string annotations from, and it is process-wide: two readers that overlap make the later one
    # replace the entry the earlier one registered, and the earlier model then resolves the other
    # execution's half-built namespace and is never completed (`PydanticUserError: <Model> is not
    # fully defined` on first instantiation). Readers do overlap in a live daemon: a config read and
    # a config write build the panel metadata on their own dispatch threads.
    with _READING:
        held = sys.modules.get(dotted)
        sys.modules[dotted] = module
        try:
            spec.loader.exec_module(module)
            contribute = object_of(getattr, module, "contribute", None)
            if contribute is None:
                raise AttributeError(f"{face_py} has no contribute()")
            if not callable(contribute):
                raise TypeError(f"{face_py} contribute is not callable")
            contributions = object_of(contribute)
        finally:
            if held is None:
                sys.modules.pop(dotted, None)
            else:
                sys.modules[dotted] = held
    if not isinstance(contributions, PluginContributions):
        raise TypeError(f"{face_py} contribute() returned {type(contributions).__name__}")
    cls = contributions.config
    if dataclasses.replace(contributions, config=None, flags=()) != PluginContributions():
        raise ValueError(f"{face_py} may declare only config and Core flags")
    if cls is not None and not (isinstance(cls, type) and issubclass(cls, BaseModel)):
        raise TypeError(f"{face_py} declared {cls!r}, which is not a BaseModel subclass")
    from base.packages.plugins.flags import validate_flag_key

    for key in contributions.flags:
        validate_flag_key(key)
    return contributions


def enabled_config_classes(plugin_dirs: dict[str, Path]) -> dict[str, type[BaseModel]]:
    """Declared config class of every plugin in `plugin_dirs` that ships a config face, fail-soft.

    A face that fails is reported (`load_report`) and its plugin contributes no class.
    """
    classes: dict[str, type[BaseModel]] = {}
    for name, plugin_dir in sorted(plugin_dirs.items()):
        try:
            cls = declared_config_class(name, plugin_dir)
        except Exception as exc:
            load_report.report_plugin_load_failure(name, exc)
            continue
        if cls is not None:
            classes[name] = cls
    return classes


def disk_image_path(plugin: str, *, home: Path) -> Path:
    from base.packages.plugin_config_images import config_image_path

    return config_image_path(plugin, home=home)


def read_config_image[C: BaseModel](cls: type[C], config_path: Path) -> C:
    try:
        content = config_path.read_text()
    except FileNotFoundError:
        return cls()

    return config_from_image(cls, content, config_path)


def write_default_disk_image(plugin: str, cls: type[BaseModel], *, home: Path) -> Path:
    from base.packages.plugin_config_images import PluginConfigOwner

    config_path = disk_image_path(plugin, home=home)
    PluginConfigOwner(plugin, cls, config_path).create_default()
    return config_path
