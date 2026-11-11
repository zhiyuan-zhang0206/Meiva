"""The module objects `meiva.skills.<name>` resolves to: a skill leaf and a namespace folder.

Both subclass `types.ModuleType` so `inspect.ismodule` is true and `meiva.help()` walks them.
What `meiva.help()` shows for one is its name, its description and the path of the file to read;
the SKILL.md body is never part of it - the agent reads the file itself.
"""

from __future__ import annotations

import types

from base.packages.skills.tree import NS, Leaf, Skill

__all__ = ["Namespace", "SkillProxy", "help_text", "node_to_obj"]


# meiva:note
# The text a skill object's help shows for itself: its description (when it has one) and the path
# of the file or folder to read.
def help_text(description: str | None, path: str | None) -> str:
    return "\n\n".join(part for part in (description, path and f"Path: {path}") if part)


class SkillProxy(types.ModuleType):
    """Runtime object returned for a skill leaf. Also a marker for the
    `meiva.help()` renderer (checked via `_meiva_skill_kind`).

    `__doc__` is the help entry: the skill's description and the path of its SKILL.md. The body
    is not loaded here; the agent reads that file.

    `path` is a plain attribute the agent can read directly (e.g. in
    f-strings inside a SKILL.md body); it is the skill's directory, hidden from `dir()`.
    `name` is the frontmatter name (may contain hyphens).

    ``__name__`` is ``meiva.skills.<path>`` so the heading and parent-
    namespace attribution see the fully-qualified name.
    """

    _meiva_skill_kind = "leaf"

    def __init__(self, path: str, info: Skill) -> None:
        super().__init__(f"meiva.skills.{path}")
        self._description = info["description"]
        self._file = f"{info['path']}/SKILL.md"
        self.path = info["path"]
        self.name = info["name"]
        self.__doc__ = help_text(self._description, self._file)

    def __dir__(self) -> list[str]:
        return []  # leaf skill — no children to list in help view

    @property
    def __all_for_meiva__(self) -> list[str]:
        """A skill leaf has no members."""
        return []


class Namespace(types.ModuleType):
    """Runtime object for a namespace node (`meiva.skills.<seg>…`). Marker for
    the `meiva.help()` renderer (checked via `_meiva_skill_kind`).

    Its attrs descend the tree — returning nested `Namespace`s or
    `SkillProxy` leaves — so `meiva.skills.<a>.<b>.<skill>` and `meiva.help(…)`
    walk naturally.

    Three shapes, each helping as description + path, then its direct children:
    a *root skill* (its folder also has a SKILL.md: its description, the SKILL.md path), a
    namespace labelled by an INDEX.md (the INDEX.md text, the folder path), and a bare folder
    (no description, the folder path)."""

    _meiva_skill_kind = "namespace"

    def __init__(self, path: str, node: NS) -> None:
        super().__init__(f"meiva.skills.{path}")
        self._path = path
        self._tree = node
        self._description: str | None
        label: str | None
        if node.skill is not None:
            self._description = label = node.skill["description"]
            self._file = f"{node.skill['path']}/SKILL.md"
            self.name = node.skill["name"]
            self.path = node.skill["path"]
        elif node.doc is not None:
            label = node.doc.strip()
            self._description = label.split("\n")[0]
            self._file = node.path
        else:
            self._description = label = None
            self._file = node.path
        self.__doc__ = help_text(label, self._file)

    def __getattr__(self, attr: str) -> SkillProxy | Namespace:
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr not in self._tree:
            raise AttributeError(
                f"meiva.skills.{self._path} has no {attr!r} (available: {sorted(self._tree)})"
            )
        return node_to_obj(f"{self._path}.{attr}", self._tree[attr])

    def __dir__(self) -> list[str]:
        return sorted(self._tree)

    @property
    def __all_for_meiva__(self) -> list[str]:
        """The skills and folders directly under this namespace."""
        return sorted(self._tree)


# meiva:note
# Resolution is exposure, not use: nothing is read here and no `skill_invoked` attribution is
# recorded (the attribution is the file read of a SKILL.md, `meiva.skills.record_skill_invoked_by_path`).
def node_to_obj(path: str, node: Leaf | NS) -> SkillProxy | Namespace:
    """Wrap a tree node: a `Leaf` → `SkillProxy`; an `NS` → `Namespace`."""
    if isinstance(node, Leaf):
        return SkillProxy(path, node.skill)
    return Namespace(path, node)
