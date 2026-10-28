from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


# meiva:note
# What the session was handed, its own data-plane home, and the configured overrides on top.
# `DSH_HOME` is the runtime's own name for that home.
def environment(
    environ: Mapping[str, str], *, dsh_home: Path, overrides: Mapping[str, str]
) -> dict[str, str]:
    merged = dict(environ)
    merged["DSH_HOME"] = str(dsh_home)
    merged.update(overrides)
    return merged
