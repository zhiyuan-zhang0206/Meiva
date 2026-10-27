# meiva:note
# A file promoted to `strict` in the same change that adds filesystem reads beside it. Strict mode is
# the end of a ratchet: the debts of the file are gone because the effect left it, and where it went
# is the question. The honest answer is a shell that runs a plan the strict module returns; the other
# answer is a lower-level read in a neighbour that nothing judges. The reads are found by vocabulary
# in the added lines of the sibling modules (the promoted module itself cannot hold one: `meizel
# check` refuses it), so a hit is a question and the trailer names the split.
from __future__ import annotations

import re
from collections.abc import Sequence

from scripts.lint.diagnostics.diff_hints.model import FileChange, HintId, Promotion, Site
from scripts.lint.diagnostics.diff_hints.scope import is_test_path

_READS = re.compile(
    r"\bos\.(?:stat|lstat|fstat|open|read|listdir|scandir|walk|readlink)\b"
    r"|\bopen\("
    r"|\.(?:read_text|read_bytes|write_text|write_bytes|stat|lstat|is_file|is_dir|exists|iterdir"
    r"|rglob|glob|resolve)\("
)
_SHOWN = 3


def strict_promotion(promotions: Sequence[Promotion], changes: Sequence[FileChange]) -> list[Site]:
    sites: list[Site] = []
    for promotion in promotions:
        directory = promotion.path.rpartition("/")[0]
        beside = [
            f"{change.path}: {text.strip()}"
            for change in changes
            if change.path != promotion.path
            and change.path.rpartition("/")[0] == directory
            and change.path.endswith(".py")
            and not is_test_path(change.path)
            for text in change.added_text
            if _READS.search(text)
        ]
        if beside:
            shown = "; ".join(f"`{read}`" for read in beside[:_SHOWN])
            more = f" and {len(beside) - _SHOWN} more" if len(beside) > _SHOWN else ""
            detail = (
                f"promoted to strict (was {promotion.was}) in {promotion.manifest} while the same "
                f"change adds reads beside it: {shown}{more}"
            )
            sites.append(Site(HintId.STRICT_PROMOTION, promotion.path, 1, detail))
    return sites
