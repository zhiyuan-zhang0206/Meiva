# meiva:note
# The files one judgment may look at: the tree being checked, asked as data.
#
# A rule reads through this instead of probing paths, so what it can see is what its caller admitted:
# the checkout under judgment, not whatever the process happens to sit in. `module.py` parses modules
# out of it and caches the parse on it; nothing here imports back.

from __future__ import annotations

from collections.abc import Callable, Iterable


# meiva:note
# The files a judgment may look at, and how to read one. A rule asks this instead of probing paths,
# so what it can see is the caller's input: the checkout it is judging, not whatever is on disk.
class Tree:
    def __init__(
        self, files: Iterable[str], read: Callable[[str], str | None], dirs: Iterable[str] = ()
    ) -> None:
        self.files = frozenset(files)
        self._read = read
        self._dirs = frozenset(dirs) | {
            "/".join(parts[:depth])
            for path in self.files
            for parts in (path.split("/"),)
            for depth in range(1, len(parts))
        }
        self.modules: dict[str, object] = {}

    def has_file(self, rel: str) -> bool:
        return rel in self.files

    def read(self, rel: str) -> str | None:
        return self._read(rel) if rel in self.files else None

    # meiva:note
    # The source file a dotted name is defined in: a module, a package door, or None.
    def module_path(self, dotted_name: str) -> str | None:
        base = dotted_name.replace(".", "/")
        for candidate in (f"{base}.py", f"{base}/__init__.py"):
            if candidate in self.files:
                return candidate
        return None

    # meiva:note
    # A first-party module: a source file, or a namespace directory it holds no file for.
    def has_module(self, dotted_name: str) -> bool:
        base = dotted_name.replace(".", "/")
        return self.module_path(dotted_name) is not None or base in self._dirs
