# meiva:link meiva_builtins/plugins/meiva_code/docs/runtime-notes.meiva.okf.md

from pathlib import Path

from base.packages.skills import git_root

# Context files surfaced to the agent, in per-directory scan order. AGENTS.md
# (Meiva's / the cross-tool standard) before CLAUDE.md (Claude Code's native).
_CONTEXT_FILENAMES = ("AGENTS.md", "CLAUDE.md")


def _collect_context_files(dirs: list[Path]) -> list[Path]:
    collected: list[Path] = []
    seen: set[Path] = set()
    for d in dirs:
        for name in _CONTEXT_FILENAMES:
            f = d / name
            if f.is_file():
                real = f.resolve()
                if real not in seen:
                    collected.append(real)
                    seen.add(real)
    return collected


def find_context_files_along_path(target: Path, *, home: Path) -> list[Path]:
    target = target.resolve()
    start = target if target.is_dir() else target.parent
    if not start.exists():
        return []

    home = home.resolve()
    repo_root = git_root(start)

    # Ancestor candidates on the path: start itself or its ancestors
    ancestors = [start, *start.parents]
    boundaries: list[Path] = []
    if home in ancestors:
        boundaries.append(home)
    if repo_root and repo_root in ancestors:
        boundaries.append(repo_root)

    if not boundaries:
        return []  # Not under a git repo or $HOME: don't walk

    # Farthest boundary = fewest parts = shallowest depth = ancestor furthest
    # from target (on the path).
    boundary = min(boundaries, key=lambda p: len(p.parts))

    # Directories from start up to (and including) the boundary, then reversed
    # to farthest→nearest so the agent sees global conventions before local.
    dirs: list[Path] = []
    current = start
    while True:
        dirs.append(current)
        if current == boundary:
            break
        if current.parent == current:  # fs root fallback, only reached if invariant breaks
            break
        current = current.parent
    dirs.reverse()

    return _collect_context_files(dirs)
