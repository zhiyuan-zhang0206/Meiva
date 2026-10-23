# meiva:note
# The saved copy of a `meiva.understand` answer, decided from values: the file name and body of
# one result, and which earlier results fall out of the ring. The shell in `answer.py` writes the
# file and deletes the stale ones.
# meiva:link meiva/understand/docs/understand.meiva.okf.md

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

__all__ = ["OutputFile", "output_file", "result_source", "slugify", "stale_outputs"]


@dataclass(frozen=True)
class OutputFile:
    name: str
    content: str


# meiva:note
# Turn a prompt into a safe filename slug.
def slugify(text: str, max_len: int = 40) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[-\s]+", "-", s)
    return s[:max_len].strip("-") or "result"


# meiva:note
# Where a result came from, for the saved header: the paths list, or literal text.
def result_source(paths: Sequence[str] | None) -> str:
    return f"paths={paths!r}" if paths is not None else "text"


# meiva:note
# The file for one result. The name carries `%Y%m%d_%H%M%S_%f` in fixed-width fields, so its
# lexical order is creation order at microsecond resolution.
def output_file(prompt: str, result: str, source: str, now: datetime) -> OutputFile:
    name = f"understand_{now.strftime('%Y%m%d_%H%M%S_%f')}_{slugify(prompt, max_len=40)}.txt"
    content = "# understand result\n"
    content += f"# prompt: {prompt}\n"
    content += f"# source: {source}\n"
    content += f"# at: {now.isoformat()}\n\n"
    content += result
    return OutputFile(name, content)


# meiva:note
# The files to delete so only the newest `keep` remain, ordered by NAME rather than mtime:
# filesystem timestamp granularity is coarse enough that files written in quick succession share
# one, and a sort on mtime would then leave it unpredictable which of the tied files gets deleted.
def stale_outputs(names: Sequence[str], keep: int) -> list[str]:
    return sorted(names)[: max(0, len(names) - keep)]
