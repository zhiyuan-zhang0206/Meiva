# meiva:note
# Where a watcher's generated script, bootstrap and launcher live, and the two disk steps of cleaning stale ones: list a directory, remove names. Which names are stale is decided by `launch_plan.stale_names`. Everything here is a function of explicit arguments (the cluster home, the temp root, the agent id, a directory, names), so a test passes a `tmp_path`.

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

__all__ = ["remove", "scan", "watchers_dir"]


def watchers_dir(home: Path, tmp_root: Path, agent_id: int) -> Path:
    # Generated watcher scripts live under the system temp dir, scoped per
    # cluster + agent — NOT in $MEIVA_HOME (the old global `watchers/` dir there
    # accumulated 180+ files and let co-agents overwrite each other's scripts,
    # 2026-08-02) and NOT in the workspace. A watcher reads its script +
    # bootstrap exactly once, at launch (runpy), so the files are ephemeral
    # carriers: temp storage is their natural home, and the OS reclaims
    # whatever a killed watcher leaves behind.
    #
    # The cluster segment matters: session ids are per-agent DB counters, so
    # two co-located clusters (each its own Postgres) allocate the same ids —
    # a tmp path keyed only on agent id would collide across clusters. The
    # cluster's identity IS its home path (AGENTS.md), so the home basename +
    # a short hash of the full path makes the segment unique per cluster.
    #
    # Not a durable index — the session is the source of truth; the file only
    # needs to outlive launch, and the bootstrap self-deletes both files when
    # the watcher exits (see _build_boot), so the dir stays empty except while
    # a watcher is actually running; stale pairs are pruned at the next launch.
    slug = home.name.lstrip(".") or "cluster"
    # sha1 is fine here: the digest is a directory-name uniquifier, not a
    # security boundary (S324).
    digest = hashlib.sha1(str(home).encode()).hexdigest()[:8]  # noqa: S324
    p = tmp_root / "meiva" / f"{slug}-{digest}" / str(agent_id) / "watchers"
    p.mkdir(parents=True, exist_ok=True)
    return p


# meiva:note
# The names in `directory` right now. The order of the reads around it carries a safety argument (see `launch_plan.stale_names`): list the directory first, the live sessions second.
def scan(directory: Path) -> tuple[str, ...]:
    return tuple(entry.name for entry in directory.iterdir())


def remove(directory: Path, names: Iterable[str]) -> None:
    for name in names:
        (directory / name).unlink(missing_ok=True)
