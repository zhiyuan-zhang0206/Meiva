"""Relative paths use your workspace by default. Coding tools resolve them against
`meiva.cwd` when available. `~/...` resolves to your home directory."""

__all_for_meiva__ = [
    "append",
    "delete",
    "edit",
    "glob",
    "read",
    "write",
]

import difflib
import errno
import glob as _glob
import os
import stat
import uuid
from pathlib import Path

from base.agents.context import MeivaContext
from base.host.atomic_io import write_text_atomic
from base.log import logger
from base.paths import workspace_dir
from meiva.sdk_surface import agent_identity, process_context
from meiva.sdk_surface.pure.media.attach.attach_plan import resolve_in
from meiva.sdk_surface.validation import coerce_str, coerce_typed
from meiva.security import scan_content


def _home() -> Path:
    """`$HOME`, the documented pre-bootstrap base for relative paths.

    Read per call, so a changed `$HOME` (test fixture / `meiva.user`-style isolation)
    takes effect immediately. A container / sandbox without `$HOME` yields
    `Path("")` or a nonexistent path; raising here beats silently falling back
    to the process cwd, which would break this module's core contract."""
    home = Path.home()
    if not (home.is_absolute() and home.is_dir()):
        raise RuntimeError(f"$HOME unavailable (Path.home() = {home!r})")
    return home


# meiva:note
# The workspace directory of agent `agent_id` under the Meiva home the bound context carries (the
# home the process's root resolved once), created when missing. The one place the SDK derives an
# agent workspace, so the shell, the sessions, the understand overflow site and the code plugin's
# cwd default all follow the context and none of them reads the process environment. It is the
# SDK's internal exit, not an agent capability: it stays out of `__all_for_meiva__`.
def agent_workspace(agent_id: int) -> Path:
    return workspace_dir(agent_id, home=process_context.current_context().require_meiva_home())


# meiva:note
# The base a relative path resolves against: the agent's workspace, or `$HOME` before a process
# identity is bound. The workspace is a framework concept, so it lives here in the SDK core -
# plugins may layer cwd tracking on top, but the no-plugin baseline must not silently fall back
# to `$HOME` (issue #1008). Before a context carrying an identity is bound (test / dev REPL
# without a bootstrap) there is no workspace; `Path.home()` is the documented pre-bootstrap base,
# read per call so a changed `$HOME` takes effect immediately. The one owner of this rule: a
# caller that resolves many paths reads the base once and passes it down. The agent id is typed
# int but is None until a bootstrap establishes it.
def workspace_base() -> Path:
    aid = agent_identity.agent_id()
    if aid is None:  # pyright: ignore[reportUnnecessaryComparison]
        return _home()
    return agent_workspace(aid)


# meiva:note
# The same base for the agent `context` acts as, over the two homes the context carries: its
# workspace under the Meiva home once an identity is established, else the invoking user's home.
# A caller that holds the process context (an agent turn, a self action) takes the base from here
# and reads no home of its own; `workspace_base()` is the form for a caller with no context in
# hand.
def workspace_base_for(context: MeivaContext) -> Path:
    aid = agent_identity.agent_id(context)
    if aid is None:
        return context.require_user_home()
    return workspace_dir(aid, home=context.require_meiva_home())


def resolve(path: str | Path) -> Path:
    """Resolve a path: expand `~`, then prepend the agent's workspace
    (`$HOME` before a process identity is bound) if still relative."""
    expanded = Path(path).expanduser()
    return expanded if expanded.is_absolute() else resolve_in(workspace_base(), expanded)


def _record_skill_read(path: Path) -> None:
    """Best-effort `skill_invoked` attribution for a file that is a loaded
    skill's SKILL.md (the `.path` + `files.read(SKILL.md)` pattern).

    Reading a skill body through the generic file API is consumption exactly
    like a `help()` render, but it bypasses the lazy proxy loader that carries
    the attribution hook — so it used to go unrecorded and the `loaded` signal
    was under-counted. The resolution happens in the skills module (only a
    mounted skill's own SKILL.md qualifies); a non-matching file is a no-op.

    Telemetry, never a failure path: any problem (the skills surface disabled
    by `MEIVA_SDK_DISABLE`, an import hiccup, a scan error) is swallowed with a
    warning — a file read must never break because attribution could not run."""
    if path.name != "SKILL.md":
        return
    try:
        from meiva import skills as skills_mod

        skills_mod.record_skill_invoked_by_path(path)
    except Exception as e:  # best-effort telemetry; never break a read
        logger.warning("skill attribution skipped for {}: {}", path, e)


def _end_from_limit(start: int | None, end: int | None, limit: int | None) -> int | None:
    """`end` as given, or derived from `limit` lines from `start`; the two are mutually exclusive."""
    if limit is None:
        return end
    if end is not None:
        raise ValueError("pass either `end` or `limit`, not both")
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    return (start or 1) + limit - 1


def _check_bounds(start: int | None, end: int | None) -> None:
    if start is not None and start < 1:
        raise ValueError(f"start must be >= 1, got {start}")
    if end is not None and end < 1:
        raise ValueError(f"end must be >= 1, got {end}")
    if start is not None and end is not None and start > end:
        raise ValueError(f"start ({start}) must be <= end ({end})")


def read(
    path: str | Path,
    start: int | None = None,
    end: int | None = None,
    *,
    limit: int | None = None,
    with_line_numbers: bool = False,
) -> str:
    """Read a file, or a 1-indexed inclusive line range.

    `limit` (max lines from `start`) is mutually exclusive with `end`.
    """
    path = coerce_str(path, "path", allow_types=(os.PathLike,))
    start = coerce_typed(start, "start", int, allow_none=True)
    end = coerce_typed(end, "end", int, allow_none=True)
    limit = coerce_typed(limit, "limit", int, allow_none=True)
    with_line_numbers = coerce_typed(with_line_numbers, "with_line_numbers", bool)
    end = _end_from_limit(start, end, limit)

    # A read is an ingestion surface: file bytes flow straight into the agent's
    # context, so the returned text is scanned for injection patterns before it
    # leaves. Clean content is returned byte-for-byte (scan_content is a no-op).
    source = f"file.read:{path}"
    p = resolve(path)
    # A SKILL.md body entering the context is skill consumption — attribute
    # it even though the generic file API bypasses the lazy proxy hook.
    _record_skill_read(p)
    text = p.read_text(encoding="utf-8")
    if start is None and end is None and not with_line_numbers:
        return scan_content(text, source=source)
    _check_bounds(start, end)
    lines = text.splitlines(keepends=True)
    lo = (start - 1) if start is not None else 0
    hi = end if end is not None else len(lines)
    selected = lines[lo:hi]
    if with_line_numbers:
        last_no = lo + len(selected)
        width = max(1, len(str(last_no)))
        numbered = "".join(f"{lo + i + 1:>{width}}: {line}" for i, line in enumerate(selected))
        return scan_content(numbered, source=source)
    return scan_content("".join(selected), source=source)


# meiva:note
# The mode a new file gets: whatever the umask gives any new file. The umask itself cannot be
# read without changing it for the whole process, so a probe file is created beside the target
# (`os.open` honors the umask) and removed again.
def _new_file_mode(directory: Path) -> int:
    probe = directory / f".{uuid.uuid4().hex}.mode"
    fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        return stat.S_IMODE(os.fstat(fd).st_mode)
    finally:
        os.close(fd)
        probe.unlink()


# meiva:note
# Replace `target` with `content` so a reader sees the old file or the new one, never half. The
# commit and its cleanup are `base.host.atomic_io`'s contract: the content goes to a temporary
# file in the same directory, which then replaces the target. A symlink target is written
# through, as an in-place write would. An existing target keeps its permission bits and must be
# writable; a new file gets the mode the umask gives any new file.
def _replace_atomically(target: Path, content: str) -> None:
    if target.is_symlink():
        target = target.resolve()
    if target.exists():
        if not os.access(target, os.W_OK):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(target))
        mode = stat.S_IMODE(target.stat().st_mode)
    else:
        mode = _new_file_mode(target.parent)
    write_text_atomic(target, content, encoding="utf-8", mode=mode, sync_file=False)


def write(path: str | Path, content: str) -> None:
    """Write, creating parent directories if needed. An existing file is replaced in one step, so
    a reader never sees a half-written file."""
    path = coerce_str(path, "path", allow_types=(os.PathLike,))
    content = coerce_str(content, "content")
    p = resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _replace_atomically(p, content)


def append(path: str | Path, content: str) -> None:
    """Append to a file, creating it (and parent directories) if absent."""
    path = coerce_str(path, "path", allow_types=(os.PathLike,))
    content = coerce_str(content, "content")
    p = resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(content)


_FUZZY_HINT_THRESHOLD = 0.4


def _closest_match_hint(content: str, old: str) -> str:
    """When `old` isn't in `content` verbatim, locate the most similar
    contiguous line range and return a one-block unified-diff hint.

    Returns the empty string when no slice is close enough (similarity
    below `_FUZZY_HINT_THRESHOLD`) or when inputs are degenerate (`old`
    empty / `content` empty / `old` longer than `content`).
    """
    content_lines = content.splitlines(keepends=True)
    old_lines = old.splitlines(keepends=True)
    if not old_lines or not content_lines or len(old_lines) > len(content_lines):
        return ""

    window_size = len(old_lines)
    best_ratio = 0.0
    best_start = -1
    matcher = difflib.SequenceMatcher(a="", b=old, autojunk=False)
    for i in range(len(content_lines) - window_size + 1):
        window = "".join(content_lines[i : i + window_size])
        matcher.set_seq1(window)
        # `quick_ratio` is an upper bound; skip the expensive call when
        # we can't possibly beat the running best.
        if matcher.quick_ratio() < best_ratio:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = i

    if best_start < 0 or best_ratio < _FUZZY_HINT_THRESHOLD:
        return ""

    s = best_start + 1  # 1-indexed inclusive
    e = best_start + window_size
    window_text = "".join(content_lines[best_start : best_start + window_size])
    diff = "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            window_text.splitlines(),
            fromfile="your `old`",
            tofile=f"actual lines {s}-{e}",
            lineterm="",
            n=3,
        )
    )
    return f"\n\nClosest match at lines {s}-{e} ({int(best_ratio * 100)}% similar):\n{diff}"


def edit(path: str | Path, old: str, new: str, *, replace_all: bool = False) -> None:
    """`old` must appear exactly once unless `replace_all=True`. When it is
    not found, the error includes a diff against the closest match in the
    file.
    """
    path = coerce_str(path, "path", allow_types=(os.PathLike,))
    old = coerce_str(old, "old")
    new = coerce_str(new, "new")
    replace_all = coerce_typed(replace_all, "replace_all", bool)
    p = resolve(path)
    content = p.read_text(encoding="utf-8")
    count = content.count(old)
    if count == 0:
        hint = _closest_match_hint(content, old)
        raise ValueError(f"old not found in {path!r}{hint}")
    if count > 1 and not replace_all:
        raise ValueError(
            f"old appears {count} times in {path!r}; pass replace_all=True to replace all"
        )
    new_content = content.replace(old, new) if replace_all else content.replace(old, new, 1)
    p.write_text(new_content, encoding="utf-8")


def glob(pattern: str = "*") -> list[Path]:
    pattern = coerce_str(pattern, "pattern")
    p = resolve(pattern)
    # Path.glob in Python 3.12 doesn't accept an absolute pattern, and
    # `**` spanning multiple levels is cleaner via stdlib
    # `glob.glob(recursive=True)` than pathlib. Can refactor on 3.13+.
    return sorted(Path(s) for s in _glob.glob(str(p), recursive=True))  # noqa: PTH207


def delete(path: str | Path) -> None:
    """Delete a file (not a directory)."""
    path = coerce_str(path, "path", allow_types=(os.PathLike,))
    resolve(path).unlink()
