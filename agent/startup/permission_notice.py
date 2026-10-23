# meiva:note
# The one notice the startup posts for the desktop-permission faults it claimed. A single fault
# keeps its own headline and diagnostic; several share one notice that names each.

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionNotice:
    title: str
    content: str


# meiva:note
# `faults` are the (headline, diagnostic) pairs of the claimed faults, at least one, in the
# order the status files are claimed.
def permission_notice(faults: Sequence[tuple[str, str]]) -> PermissionNotice:
    if not faults:
        raise ValueError("a permission notice needs at least one fault")
    if len(faults) == 1:
        headline, diagnostic = faults[0]
        return PermissionNotice(title=headline, content=diagnostic)
    return PermissionNotice(
        title="Desktop permissions missing",
        content="\n\n".join(f"{headline}: {diagnostic}" for headline, diagnostic in faults),
    )
