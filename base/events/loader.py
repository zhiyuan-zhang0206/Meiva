"""Merge the per-domain event declarations of ``base/events/declarations/``.

``load_events`` imports every module of that package and merges their ``EVENTS``, so a new
domain is a new file there with nothing else to register.
"""

import importlib
import pkgutil
from collections.abc import Iterable, Mapping

from base.events import declarations
from base.events.vocabulary import EventSpec
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of


def merge_events(modules: Iterable[tuple[str, Mapping[str, EventSpec]]]) -> dict[str, EventSpec]:
    """Merge ``(module name, events)`` pairs in order; a repeated or mis-keyed name is an error."""
    merged: dict[str, EventSpec] = {}
    owner: dict[str, str] = {}
    for module, events in modules:
        for name, spec in events.items():
            if spec.name != name:
                raise ValueError(f"{module}: key {name!r} holds the spec for {spec.name!r}")
            if name in merged:
                raise ValueError(f"event {name!r} is declared in both {owner[name]} and {module}")
            merged[name] = spec
            owner[name] = module
    return merged


# meiva:note
# The `EVENTS` table of one declarations module. Importing by name yields a module typed `Any`; the table
# is read through `object_of` and must be a mapping of event name to `EventSpec`.
def _declared_events(name: str) -> Mapping[str, EventSpec]:
    module = importlib.import_module(f"{declarations.__name__}.{name}")
    events = object_of(getattr, module, "EVENTS")
    if not is_object_dict(events):
        raise TypeError(
            f"{declarations.__name__}.{name}.EVENTS must be a mapping of names to events"
        )
    table: dict[str, EventSpec] = {}
    for key, spec in events.items():
        if not isinstance(spec, EventSpec):
            raise TypeError(f"{declarations.__name__}.{name}.EVENTS[{key!r}] is not an EventSpec")
        table[key] = spec
    return table


def load_events() -> dict[str, EventSpec]:
    """Every declared event, merged in module-name order."""
    names = sorted(info.name for info in pkgutil.iter_modules(declarations.__path__))
    return merge_events((name, _declared_events(name)) for name in names)
