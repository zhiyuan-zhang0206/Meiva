import contextlib
from pathlib import Path

from base.agents.workspace_files.errors import (
    WorkspaceFileFailure,
    WorkspaceFileRefused,
)
from base.paths import workspace_path


# meiva:note
# The two places a file may come from: the agent's workspace, and the directory its last turn ran
# in when that is somewhere else. The cwd is first because a relative path a reader sees in a
# transcript is relative to it; the workspace is the fallback the panel links point at.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
def allowed_roots(
    home: Path,
    agent_id: int,
    custom_cwd: str | None,
) -> tuple[list[Path], Path]:
    workspace_dir = workspace_path(home, agent_id).resolve()
    cwd_dir = workspace_dir
    if custom_cwd:
        candidate_cwd = Path(custom_cwd).resolve()
        if candidate_cwd.exists() and candidate_cwd.is_dir():
            cwd_dir = candidate_cwd

    roots: list[Path] = []
    if cwd_dir.exists():
        roots.append(cwd_dir)
    if workspace_dir.exists() and workspace_dir != cwd_dir:
        roots.append(workspace_dir)

    if not roots:
        roots = [cwd_dir]
        if workspace_dir != cwd_dir:
            roots.append(workspace_dir)

    return roots, cwd_dir


# meiva:note
# The panel links carry `#L12-L15` and `?x=1` suffixes and may arrive as `file://` URLs; none of
# them names a path, so each is stripped before resolution. A refused path is a class of its own
# so the HTTP layer can answer 400 rather than pretending the file is missing.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
def clean_file_path(raw_path: str) -> str:
    clean = raw_path.split("#", 1)[0].split("?", 1)[0].strip()
    if not clean:
        raise WorkspaceFileRefused(WorkspaceFileFailure.PATH_INVALID, "Path cannot be empty")
    if clean.startswith("file://"):
        clean = clean[len("file://") :]
    if "\0" in clean:
        raise WorkspaceFileRefused(WorkspaceFileFailure.PATH_INVALID, "Path carries a null byte")
    return clean


# meiva:note
# An absolute path is honoured only when it resolves inside a root; otherwise it is retried as if
# it were relative, because a transcript prints absolute paths that belong to the machine the turn
# ran on and a client may replay one against a different mount.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
def resolve_absolute_path(
    clean_p: Path,
    clean_str: str,
    allowed_roots: list[Path],
    primary_dir: Path,
) -> Path:
    with contextlib.suppress(ValueError, RuntimeError):
        resolved = clean_p.resolve()
        for root in allowed_roots:
            if resolved.is_relative_to(root) and resolved != root:
                return resolved

    stripped = clean_str.lstrip("/\\")
    for root in allowed_roots:
        candidate = (root / stripped).resolve()
        if candidate.is_relative_to(root) and candidate != root and candidate.exists():
            return candidate

    candidate = (primary_dir / stripped).resolve()
    if candidate.is_relative_to(primary_dir) and candidate != primary_dir:
        return candidate

    raise WorkspaceFileRefused(
        WorkspaceFileFailure.OUTSIDE_ROOTS,
        "Path is outside the agent workspace and cwd",
    )


# meiva:note
# A relative path is tried against the cwd first and every other root after it: the common case is
# a file the agent just wrote, and the workspace is where a link from the panel lands.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
def resolve_relative_path(
    clean: str,
    allowed_roots: list[Path],
    primary_dir: Path,
) -> Path:
    candidate = (primary_dir / clean).resolve()
    if candidate.is_relative_to(primary_dir) and candidate != primary_dir and candidate.exists():
        return candidate

    for root in allowed_roots:
        if root != primary_dir:
            alt = (root / clean).resolve()
            if alt.is_relative_to(root) and alt != root and alt.exists():
                return alt

    if candidate.is_relative_to(primary_dir) and candidate != primary_dir:
        return candidate

    for root in allowed_roots:
        alt = (root / clean).resolve()
        if alt.is_relative_to(root) and alt != root:
            return alt

    raise WorkspaceFileRefused(
        WorkspaceFileFailure.OUTSIDE_ROOTS,
        "Path is outside the agent workspace and cwd",
    )


# meiva:note
# The one entry point both the machine that owns the disk and the gateway that serves it call, so a
# path means the same thing whichever side answers the request.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
def resolve_workspace_file_path(
    allowed_roots: list[Path],
    primary_dir: Path,
    raw_path: str,
) -> Path:
    clean = clean_file_path(raw_path)
    clean_p = Path(clean)
    if clean_p.is_absolute():
        return resolve_absolute_path(clean_p, clean, allowed_roots, primary_dir)
    return resolve_relative_path(clean, allowed_roots, primary_dir)
