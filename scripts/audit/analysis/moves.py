# meiva:note
# What a move leaves behind in text: references to the old path, names reached at the new one.
#
# `module_moves.py` reads the checkout and imports the new module; deciding which lines still name
# the old path, and which names a caller uses at the new one, is here.

from __future__ import annotations

import re
from typing import TypeGuard

__all__ = [
    "is_text",
    "module_pair",
    "old_references",
    "pair_result",
    "referenced_names",
    "verdict_line",
]

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


# meiva:note
# The `OLD=NEW` pair an operator names on the command line: two dotted module names, both
# non-empty, each segment an identifier.
def module_pair(value: str) -> tuple[str, str]:
    parts = value.split("=")
    if len(parts) != 2 or any(
        not all(part.isidentifier() for part in module.split(".")) for module in parts
    ):
        raise ValueError("expected OLD=NEW with dotted Python module names")
    return parts[0], parts[1]


# meiva:note
# Whether a tracked file's text is content this audit reads: a binary file carries a NUL byte and
# is skipped, an unreadable one has no text at all.
def is_text(text: str | None) -> TypeGuard[str]:
    return text is not None and "\0" not in text


# meiva:note
# What one pair reports: the line naming its old references and missing names, and whether it
# failed the audit (any old reference, any missing name, any import error).
def pair_result(
    old: str,
    new: str,
    *,
    old_count: int,
    names: int,
    missing: list[str],
    import_error: str,
) -> tuple[str, bool]:
    line = (
        f"{old} -> {new}: {old_count} old references; "
        f"{names} referenced names, missing={missing}{import_error}"
    )
    return line, bool(old_count or missing or import_error)


# meiva:note
# The verdict line the audit ends with, and the exit status it stands for.
def verdict_line(*, failed: bool) -> str:
    return f"AUDIT: {'FAIL' if failed else 'PASS'}"
