"""Relative paths use your workspace by default. Coding tools resolve them against
`meiva.cwd` when available. `~/...` resolves to your home directory."""

__all_for_meiva__ = [
    "append",
    "delete",
    "edit",
    "glob",
    "read",
    "restore",
    "write",
]

import difflib
import errno
import glob as _glob
import os
import re
import shutil
import stat
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from base.config import settings
from base.log import logger
from base.paths import meiva_home, trash_dir, workspace_dir
from base.paths.private_dir import ensure_private_dir
from meiva.sdk_surface import agent_identity
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


def resolve(path: str | Path) -> Path:
    """Resolve a path: expand `~`, then prepend the agent's workspace
    (`$HOME` before a process identity is bound) if still relative."""
    # `expanduser` translates `~` / `~user`; if still a relative path,
    # prepend the per-agent workspace. The workspace is a framework
    # concept, so it lives here in the SDK core — plugins may layer cwd
    # *tracking* on top, but the no-plugin baseline must not silently
    # fall back to `$HOME` (issue #1008). Before a context
    # carrying an identity is bound (test / dev REPL without a bootstrap) there is
    # no workspace; `Path.home()` is the documented pre-bootstrap base
    # (per-call live, so test fixture mock env takes effect immediately).
    p = Path(path).expanduser()
    if not p.is_absolute():
        aid = agent_identity.agent_id()
        # agent id is typed int but is None until a bootstrap establishes it.
        base = workspace_dir(aid) if aid is not None else _home()  # pyright: ignore[reportUnnecessaryComparison]
        p = base / p
    return p


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


def _replace_atomically(target: Path, content: str) -> None:
    """Replace `target` with `content` so a reader sees the old file or the new one, never half.

    The content goes to a temporary file in the same directory (same filesystem, so the rename is
    atomic), which then replaces the target. A symlink target is written through, as an in-place
    write would. An existing target keeps its permission bits and must be writable; a new file gets
    the mode the umask gives any new file. A failure at any point removes the temporary file and
    leaves the target as it was."""
    if target.is_symlink():
        target = target.resolve()
    existing_mode: int | None = None
    if target.exists():
        if not os.access(target, os.W_OK):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(target))
        existing_mode = stat.S_IMODE(target.stat().st_mode)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        # os.open honors the umask for a new file's mode, as open() does.
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(content)
        if existing_mode is not None:
            temporary.chmod(existing_mode)
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


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


_TRASH_BUCKET = re.compile(r"(\d{4}-\d{2}-\d{2})(?:\.\d+)?")


def _trash_root(home: Path | None = None, *, create: bool = False) -> Path:
    """The trash tree one home keeps (`$MEIVA_HOME/trash`), symlinks resolved.

    Every path this module builds and parses goes through here, so a delete and a
    later restore agree on the prefix even when the home is reached through a
    symlink. `create=True` makes the directory owner-only and re-checks that it
    still is: a trash that became a symlink or a foreign directory is refused
    rather than written through."""
    root = trash_dir(home if home is not None else meiva_home())
    if create:
        ensure_private_dir(root)
    return Path(os.path.realpath(root))


def _machine_label() -> str:
    """This machine's layer in the trash: the configured name, never the hostname.

    Machine names are deployment labels (`settings.general.machine_name`). An
    uninitialized home — a test, a bare REPL — has none and gets `local`; the tree
    is per home, so that bucket cannot collide across machines. A value that is not
    one plain path component also falls back: it must not steer the layout."""
    label = settings.general.machine_name.strip()
    if not label or label in {".", ".."} or "/" in label or "\\" in label:
        return "local"
    return label


def _absolute(path: Path) -> Path:
    """`path` as a lexical absolute path: `..` collapsed, symlinks untouched.

    The kernel resolves `..` after following a symlink; the payload's recorded
    origin is also the path the move itself acts on, so the two always agree."""
    # meiva:note
    # `Path.resolve()` follows symlinks and would record a link's target instead of
    # the link the delete actually moves; `..` must collapse lexically here.
    # meiva:link meiva/docs/files.meiva.okf.md
    return Path(os.path.abspath(path))  # noqa: PTH100


def _today() -> date:
    """The local calendar day a delete or a purge reasons about.

    Local, not UTC: the day layer is what a person reads when they go looking for
    a file deleted this morning. The caller may pin it (`today=`) in tests."""
    return datetime.now().astimezone().date()


def _relative_to_root(origin: Path) -> Path:
    """The part of an absolute path below its filesystem root — the payload's path
    inside a trash entry. Refuses a path with no such part or a `..` component."""
    relative = origin.relative_to(origin.anchor)
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"cannot trash {str(origin)!r}: no path below its filesystem root")
    return relative


def _trash_entry(origin: Path, *, today: date | None = None) -> Path:
    """The trash path a delete of `origin` lands in, unique for the day.

    `<trash>/<machine>/<YYYY-MM-DD>/<origin without its root>`; while that path is
    already taken by an earlier delete of the same origin, the day bucket moves on
    (`<YYYY-MM-DD>.2`, `.3`, …). The payload therefore always keeps its exact
    origin path, which is what lets `restore` recover it without a manifest."""
    relative = _relative_to_root(origin)
    machine_dir = _trash_root(create=True) / _machine_label()
    day = (today or _today()).isoformat()
    index = 1
    while True:
        bucket = machine_dir / (day if index == 1 else f"{day}.{index}")
        entry = bucket / relative
        if not os.path.lexists(entry):
            return entry
        index += 1


def move_file(
    src: Path,
    dst: Path,
    *,
    replace: Callable[[Path, Path], None] = os.replace,
    copy: Callable[..., object] = shutil.copy2,
) -> None:
    """Move `src` to `dst` so `src` is never lost.

    Same filesystem: one atomic rename. Across devices (EXDEV): copy to a
    temporary sibling of `dst`, flush it, rename it into place, and only then
    unlink `src` — a failure before that rename leaves `src` untouched, and a
    crash after it leaves both copies rather than neither.

    `replace` and `copy` are the two filesystem primitives the move needs; they
    are parameters so the cross-device drill can pass a fake in instead of
    mounting a second filesystem. The trash passes the defaults."""
    dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        replace(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    temporary = dst.with_name(f".{dst.name}.{uuid.uuid4().hex}.tmp")
    try:
        copy(src, temporary, follow_symlinks=False)
        if not temporary.is_symlink():
            descriptor = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        replace(temporary, dst)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    src.unlink()


def delete(path: str | Path) -> None:
    """Move a file to the trash instead of unlinking it, so a wrong delete stays recoverable.

    The file leaves its path immediately and lands under
    `$MEIVA_HOME/trash/<machine>/<YYYY-MM-DD>/<its path without the filesystem root>`;
    a second delete of the same path that day uses the next bucket
    (`<YYYY-MM-DD>.2`). `restore` moves it back; entries past the configured
    retention are purged by the machine's boot maintenance. Directories are refused.
    """
    path = coerce_str(path, "path", allow_types=(os.PathLike,))
    origin = _absolute(resolve(path))
    if not os.path.lexists(origin):
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(origin))
    if origin.is_dir() and not origin.is_symlink():
        raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(origin))
    move_file(origin, _trash_entry(origin))


def _trash_origin(entry: Path) -> Path:
    """The absolute path a trash entry restores to, read from its place in the tree.

    The entry's parent is resolved — not the entry, which may itself be a symlink
    that must be restored as a link — then `<machine>/<day bucket>` is checked and
    dropped, leaving the original path below its filesystem root."""
    root = _trash_root()
    resolved = Path(os.path.realpath(entry.parent)) / entry.name
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"{str(entry)!r} is not inside the trash ({root})") from None
    parts = relative.parts
    if len(parts) < 3 or _TRASH_BUCKET.fullmatch(parts[1]) is None:
        raise ValueError(
            f"{str(entry)!r} is not a trash entry: expected "
            f"{root}/<machine>/<YYYY-MM-DD>/<original path>"
        )
    return Path(entry.anchor).joinpath(*parts[2:])


def restore(path: str | Path) -> Path:
    """Move a file back out of the trash to the path it was deleted from.

    `path` is the file's path inside the trash, exactly as `delete` leaves it.
    Fails when the original path exists again — restore never overwrites — and for
    a path that is not a trash entry. Returns the restored path.
    """
    path = coerce_str(path, "path", allow_types=(os.PathLike,))
    entry = _absolute(resolve(path))
    origin = _trash_origin(entry)
    if not os.path.lexists(entry):
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(entry))
    if entry.is_dir() and not entry.is_symlink():
        raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(entry))
    if os.path.lexists(origin):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(origin))
    origin.parent.mkdir(parents=True, exist_ok=True)
    move_file(entry, origin)
    return origin


def _bucket_day(name: str) -> date | None:
    """The day a trash bucket directory stands for, or None when it is not one."""
    match = _TRASH_BUCKET.fullmatch(name)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _real_subdirectories(directory: Path) -> list[Path]:
    """Direct child directories that are directories themselves — never a symlink."""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    return sorted(Path(e.path) for e in entries if e.is_dir(follow_symlinks=False))


def _remove_bucket(bucket: Path) -> int:
    """Remove one expired day bucket; return how many files it held."""
    files = 0
    for _directory, _subdirectories, names in os.walk(bucket, followlinks=False):
        files += len(names)
    shutil.rmtree(bucket, ignore_errors=True)
    return files


def sweep_trash(retention_days: int, *, home: Path | None = None, today: date | None = None) -> int:
    """Purge trash entries older than `retention_days`; return the files removed.

    The machine's boot maintenance calls this next to the trace-mirror prune. It
    reads and removes nothing outside the trash tree: only `<machine>` and day
    buckets that are real directories are entered, and `shutil.rmtree` removes a
    symlink instead of descending through it. `retention_days <= 0` disables the
    pass (nothing is provably expired)."""
    if retention_days <= 0:
        return 0
    raw = trash_dir(home if home is not None else meiva_home())
    if raw.is_symlink() or not raw.is_dir():
        return 0
    root = _trash_root(home)
    cutoff = (today or _today()) - timedelta(days=retention_days)
    removed = 0
    for machine in _real_subdirectories(root):
        for bucket in _real_subdirectories(machine):
            day = _bucket_day(bucket.name)
            if day is not None and day < cutoff:
                removed += _remove_bucket(bucket)
    if removed:
        logger.info(
            "purged expired trash entries",
            event="trash",
            files=removed,
            cutoff=cutoff.isoformat(),
        )
    return removed
