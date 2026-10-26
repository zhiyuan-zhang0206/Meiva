"""The module objects `meiva.skills.<name>` resolves to: a skill leaf and a namespace folder.

Both subclass `types.ModuleType` so `inspect.ismodule` is true and `meiva.help()` walks them.
The one thing they need from outside is how a skill's SKILL.md body is consumed (read it and
record the `skill_invoked` attribution): the facade passes that as `consume`, so a test passes
a function that returns a body and records nothing.
"""

from __future__ import annotations

import types
from collections.abc import Callable

from base.packages.skills.tree import NS, Leaf, Skill

__all__ = ["Consume", "LazySkillDoc", "Namespace", "SkillProxy", "consumed_text", "node_to_obj"]

type Consume = Callable[[Skill], str]


class _LazyDocDescriptor:
    def __init__(self, class_doc: str | None = None) -> None:
        self._class_doc = class_doc

    def __get__(
        self, instance: LazySkillDoc | None, owner: type[LazySkillDoc] | None = None
    ) -> str | None:
        del owner
        if instance is None:
            return self._class_doc
        if instance._doc is None:
            return instance._load_skill_doc()
        return instance._doc

    def __set__(self, instance: LazySkillDoc, value: str | None) -> None:
        instance._doc = value


class LazySkillDoc:
    """Mixin for skill proxies: `__doc__` loads the SKILL.md body on first
    consumption (a help() render or a direct read) and records the
    `skill_invoked` attribution there — see the module note on attribution
    timing. `__init__` must set `self._doc` to None when the node carries a
    body (index-only nodes set it eagerly in `__init__` and never hit the
    lazy path) and `self._info` to the `Skill` metadata.

    Each subclass receives a `_LazyDocDescriptor` on `__doc__` via
    `__init_subclass__`: every class body auto-assigns its own docstring to
    `__doc__`, so a base-class property named `__doc__` would be shadowed by
    the subclass's own class attribute."""

    _info: Skill
    _consume: Consume
    _doc: str | None = None

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        type.__setattr__(cls, "__doc__", _LazyDocDescriptor(cls.__doc__))

    def _load_skill_doc(self) -> str:
        """Read the SKILL.md body, record the `skill_invoked` attribution, and
        cache the result in `_doc`. Runs at most once per proxy (the cache
        makes later reads free, so one proxy records once)."""
        doc = self._consume(self._info)
        self._doc = doc
        return doc


class SkillProxy(LazySkillDoc, types.ModuleType):
    """Runtime object returned for a skill leaf. Also a marker for the
    `meiva.help()` renderer (checked via `_meiva_skill_kind`).

    `__doc__` carries the skill's full content: path line (tilde-shortened)
    followed by the raw SKILL.md body — so `meiva.help(proxy)` renders the
    whole skill without synthetic Python-assignment wrapping. The body loads
    lazily on first `__doc__` access, which is also where the
    `skill_invoked` attribution is recorded (see `LazySkillDoc`).

    `path` is a plain attribute the agent can read directly (e.g. in
    f-strings inside a SKILL.md body); it is hidden from `dir()` and the
    help view so only the content is rendered. `name` is the frontmatter
    name (may contain hyphens).

    ``__name__`` is ``meiva.skills.<path>`` so the heading and parent-
    namespace attribution see the fully-qualified name.
    """

    _meiva_skill_kind = "leaf"

    def __init__(self, path: str, info: Skill, consume: Consume) -> None:
        super().__init__(f"meiva.skills.{path}")
        self._consume = consume
        self._info = info
        self._description = info["description"]
        self.path = info["path"]
        self.name = info["name"]
        self._doc = None  # lazy — SKILL.md body loads on first consumption

    def __dir__(self) -> list[str]:
        return []  # leaf skill — no children to list in help view

    @property
    def __all_for_meiva__(self) -> list[str]:
        """A skill leaf has no members; its body is its `__doc__`."""
        return []


class Namespace(LazySkillDoc, types.ModuleType):
    """Runtime object for a namespace node (`meiva.skills.<seg>…`). Marker for
    the `meiva.help()` renderer (checked via `_meiva_skill_kind`).

    Its attrs descend the tree — returning nested `Namespace`s or
    `SkillProxy` leaves — so `meiva.skills.<a>.<b>.<skill>` and `meiva.help(…)`
    walk naturally.

    A *root skill* node (its folder also has a SKILL.md): `__doc__` carries
    the path line + full SKILL.md body, and children render underneath. The
    body loads lazily on first `__doc__` access — the same attribution point
    as `SkillProxy` — so merely resolving the namespace records nothing. For
    a pure namespace (INDEX.md or no descriptor), `__doc__` is the namespace
    description, already in memory from the scan."""

    _meiva_skill_kind = "namespace"

    def __init__(self, path: str, node: NS, consume: Consume) -> None:
        super().__init__(f"meiva.skills.{path}")
        self._consume = consume
        self._path = path
        self._tree = node
        if node.skill is not None:
            self._info = node.skill
            self._doc = None  # lazy — SKILL.md body loads on first consumption
            self._description = node.skill["description"]
            self.name = node.skill["name"]
            self.path = node.skill["path"]
        elif node.doc is not None:
            self._doc = node.doc
        else:
            children = sorted(node)
            self._doc = f"Contains: {', '.join(children)}"

    def __getattr__(self, attr: str) -> SkillProxy | Namespace:
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr not in self._tree:
            raise AttributeError(
                f"meiva.skills.{self._path} has no {attr!r} (available: {sorted(self._tree)})"
            )
        return node_to_obj(f"{self._path}.{attr}", self._tree[attr], self._consume)

    def __dir__(self) -> list[str]:
        return sorted(self._tree)

    @property
    def __all_for_meiva__(self) -> list[str]:
        """The skills and folders directly under this namespace."""
        return sorted(self._tree)


def node_to_obj(path: str, node: Leaf | NS, consume: Consume) -> SkillProxy | Namespace:
    """Wrap a tree node: a `Leaf` → `SkillProxy`; an `NS` → `Namespace`.

    Resolution is exposure, not use: no SKILL.md body is read here and no
    `skill_invoked` attribution is recorded. Index renders and system-prompt
    assembly resolve nodes every turn (assembly walks `_flatten` directly) —
    recording here made exposure look like use: ~70% of "loaded" rows had no
    usage trace in the transcript. Attribution fires on first body
    consumption, in the lazy `__doc__` loaders (`_LazySkillDoc`)."""
    if isinstance(node, Leaf):
        return SkillProxy(path, node.skill, consume)
    return Namespace(path, node, consume)


# meiva:note
# The text a consumed skill body hands over: the skill's directory with the home directory
# shortened to `~`, a blank line, then the raw SKILL.md body.
def consumed_text(skill_path: str, home: str, content: str) -> str:
    short = f"~{skill_path[len(home) :]}" if skill_path.startswith(home + "/") else skill_path
    return f"{short}\n\n{content}"
