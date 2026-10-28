# meiva:note
# What a move leaves behind in text: references to the old path, names reached at the new one.
#
# `module_moves.py` reads the checkout and imports the new module; deciding which lines still name
# the old path, and which names a caller uses at the new one, is here.

from __future__ import annotations

import re

__all__ = ["old_references", "referenced_names"]

_HISTORY_READ_RE = re.compile(r"""git\s+show\s+(?:"[^"]*"|'[^']*'|\S+)""")


def _mask_history_reads(text: str) -> str:
    # meiva:note
    # Blank `git show <rev>:<path>` operands — object reads, never live references.
    return _HISTORY_READ_RE.sub(lambda match: " " * len(match.group(0)), text)


def _from_imports(module: str, text: str) -> list[tuple[int, set[str]]]:
    # meiva:note
    # Import names and offsets, including multiline imports in prose snippets.
    if module not in text or "from" not in text:
        return []
    pattern = re.compile(
        rf"(?<![\w.])from\s+{re.escape(module)}\s+import\s+"
        r"(\((?:#[^\n]*|[^)#])*\)|(?:\\\n|[^\n;])+)"
    )
    imports: list[tuple[int, set[str]]] = []
    for match in pattern.finditer(text):
        body = re.sub(r"#[^\n]*", "", match.group(1)).strip("()")
        body = body.replace("\\\n", "")
        names = set(re.findall(r"(?:^|,)\s*([A-Za-z_]\w*|\*)", body))
        imports.append((match.start(), names))
    return imports


def old_references(module: str, text: str) -> list[int]:
    text = _mask_history_reads(text)
    pattern = re.compile(rf"(?<![\w.]){re.escape(module)}(?!\w)")
    offsets = [match.start() for match in pattern.finditer(text)] if module in text else []
    parent, _, leaf = module.rpartition(".")
    if parent:
        offsets.extend(offset for offset, names in _from_imports(parent, text) if leaf in names)
    slash = module.replace(".", "/")
    if slash in text:
        slash_pattern = re.compile(rf"(?<![\w.]){re.escape(slash)}\.py(?!\w)")
        offsets.extend(match.start() for match in slash_pattern.finditer(text))
    return sorted(text.count("\n", 0, offset) + 1 for offset in offsets)


def referenced_names(module: str, text: str) -> set[str]:
    text = _mask_history_reads(text)
    if module not in text:
        return set()
    refs = set(re.findall(rf"(?<![\w.]){re.escape(module)}\.([A-Za-z_]\w*)", text))
    for _, names in _from_imports(module, text):
        refs.update(names)
    return refs
