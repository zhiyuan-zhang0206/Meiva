import contextlib
import itertools
import mimetypes
import os
import posixpath
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from base.agents.to_user import (
    FileRoot,
    ToUserDocument,
    ToUserLimits,
    WorkspaceDirEntry,
    WorkspaceDirListing,
    WorkspaceFileRequest,
    WorkspaceFileResponse,
    read_document,
    read_failure,
)
from base.config.domains.to_user import FileAccess
from base.daemon.http_transport import RouteAnswer
from base.paths import WORKSPACES_DIR_NAME, workspace_path, workspaces_path

MAX_WORKSPACE_FILE_BYTES = 1024 * 1024
_IMAGE_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".bmp",
        ".ico",
    }
)
_MEDIA_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".webm",
        ".mov",
        ".mkv",
        ".mp3",
        ".wav",
        ".ogg",
        ".m4a",
        ".flac",
        ".pdf",
    }
)


class WorkspaceFileError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def read_to_user(
    pool: ConnectionPool,
    agent_id: int,
    machine: str,
    *,
    files: list[str],
    home: Path,
    limits: ToUserLimits,
) -> ToUserDocument:
    return read_document(
        owned_workspace(pool, agent_id, machine, home=home), limits=limits, files=files
    )


# meiva:note
# The gateway owns what the reader sees, so only the class crosses the wire as
# `reason`; `error` carries the local detail (exception type, budget, relative name)
# for the gateway's log.
# meiva:link gateway/agents/docs/to-user-read-failures.md
def read_failure_payload(exc: OSError | ValueError) -> dict[str, object]:
    return {"error": f"{type(exc).__name__}: {exc}", "reason": read_failure(exc).value}


def owned_workspace(pool: ConnectionPool, agent_id: int, machine: str, *, home: Path) -> Path:

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None or row[0] != machine:
        raise ValueError("To user can only be read on the agent's home machine")
    return workspace_path(home, agent_id)


def agent_allowed_roots(
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


_EXPLICIT_WORKSPACE_RE = re.compile(
    rf"^.*?(?:^|/)\.meiva/{re.escape(WORKSPACES_DIR_NAME)}/([1-9]\d*)(?:/([\s\S]*))?$"
)


def clean_file_path(raw_path: str) -> str:
    clean = raw_path.split("#", 1)[0].split("?", 1)[0].strip()
    if not clean:
        raise WorkspaceFileError(400, "Path parameter cannot be empty")
    if clean[:7].lower() == "file://":
        clean = clean[7:]
        if clean[:10].lower() == "localhost/":
            clean = clean[9:]
        clean = unquote(clean).strip()
    if not clean:
        raise WorkspaceFileError(400, "Path parameter cannot be empty")
    if "\0" in clean:
        raise WorkspaceFileError(400, "Invalid path: null bytes not allowed")
    return clean


def _normalize_workspace_subpath(raw_subpath: str, *, allow_root: bool) -> str:
    parts: list[str] = []
    for segment in raw_subpath.replace("\\", "/").split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                raise WorkspaceFileError(
                    403, "Access denied: path is outside the agent workspace and cwd"
                )
            _ = parts.pop()
            continue
        parts.append(segment)
    if not parts and not allow_root:
        raise WorkspaceFileError(403, "Access denied: path is outside the agent workspace and cwd")
    return "/".join(parts)


# meiva:note
# An explicit `.meiva/workspaces/<owner>/<subpath>` (or `<home>/workspaces/<owner>/<subpath>`)
# names that owner's workspace across machines and OS usernames; `..` traversal escaping above
# the workspace root is refused at the boundary instead of falling through to another directory.
# A file read refuses the root itself; a directory read (`allow_root`) answers it as `""`.
# meiva:link gateway/agents/docs/to-user-resources.md
def parse_explicit_workspace_path(
    raw_path: str, *, home: Path | None = None, allow_root: bool = False
) -> tuple[int, str] | None:
    clean = clean_file_path(raw_path)
    match = _EXPLICIT_WORKSPACE_RE.match(clean)
    if match is not None:
        return int(match.group(1)), _normalize_workspace_subpath(
            match.group(2) or "", allow_root=allow_root
        )
    if home is not None:
        prefixes = {
            f"{workspaces_path(home).as_posix().rstrip('/')}/",
            f"{workspaces_path(home).resolve().as_posix().rstrip('/')}/",
        }
        for prefix in prefixes:
            if clean.startswith(prefix):
                remainder = clean[len(prefix) :]
                owner_part, has_sep, rest = remainder.partition("/")
                if owner_part.isdigit() and not owner_part.startswith("0"):
                    return int(owner_part), _normalize_workspace_subpath(
                        rest if has_sep else "", allow_root=allow_root
                    )
    return None


# meiva:note
# Whether a resolved path lies under `root`. Symlinks are already followed by `resolve()`, so a
# link that leaves the root lands outside it; the root itself counts only for a directory read.
def _within(path: Path, root: Path, *, allow_root: bool) -> bool:
    return path.is_relative_to(root) and (allow_root or path != root)


def _resolve_absolute_path(
    clean_p: Path,
    allowed_roots: list[Path],
    *,
    allow_root: bool = False,
) -> Path:
    with contextlib.suppress(ValueError, RuntimeError, OSError):
        resolved = clean_p.resolve()
        for root in allowed_roots:
            if _within(resolved, root, allow_root=allow_root):
                return resolved

    raise WorkspaceFileError(403, "Access denied: path is outside the agent workspace and cwd")


def _resolve_relative_path(
    clean: str,
    allowed_roots: list[Path],
    primary_dir: Path,
    *,
    allow_root: bool = False,
) -> Path:
    candidate = (primary_dir / clean).resolve()
    if _within(candidate, primary_dir, allow_root=allow_root) and candidate.exists():
        return candidate

    for root in allowed_roots:
        if root != primary_dir:
            alt = (root / clean).resolve()
            if _within(alt, root, allow_root=allow_root) and alt.exists():
                return alt

    if _within(candidate, primary_dir, allow_root=allow_root):
        return candidate

    for root in allowed_roots:
        alt = (root / clean).resolve()
        if _within(alt, root, allow_root=allow_root):
            return alt

    raise WorkspaceFileError(403, "Access denied: path is outside the agent workspace and cwd")


def resolve_workspace_file_path(
    allowed_roots: list[Path],
    primary_dir: Path,
    raw_path: str,
    *,
    allow_root: bool = False,
) -> Path:
    clean = clean_file_path(raw_path)
    clean_p = Path(clean)
    if clean_p.is_absolute():
        return _resolve_absolute_path(clean_p, allowed_roots, allow_root=allow_root)
    return _resolve_relative_path(clean, allowed_roots, primary_dir, allow_root=allow_root)


# meiva:note
# What one read may reach on the machine that owns the path: its Meiva home (agent workspaces
# live under it), the account home (`~` and the `home` root), and the gateway's `file_access`.
@dataclass(frozen=True)
class FileReach:
    home: Path
    user_home: Path
    access: FileAccess


# meiva:note
# `..` in an absolute path is resolved by its text before a workspace name is read from it, so in
# `all` reach a path that leaves one workspace names wherever it lands, not the workspace it left.
def lexical_path(clean: str) -> str:
    return posixpath.normpath(clean) if clean.startswith("/") else clean


# meiva:note
# Any path in `all` reach: an explicit workspace path lands in that workspace on this machine,
# `~` in the account home, an absolute path where it names, and a relative one under the agent's
# working directory or else its workspace, as a workspace read resolves it.
def _reach_any(reach: FileReach, agent_id: int, raw_path: str, custom_cwd: str | None) -> Path:
    clean = lexical_path(clean_file_path(raw_path or "."))
    explicit = parse_explicit_workspace_path(clean, home=reach.home, allow_root=True)
    if explicit is not None:
        return (workspace_path(reach.home, explicit[0]) / explicit[1]).resolve()
    if clean == "~" or clean.startswith("~/"):
        return (reach.user_home / clean[2:]).resolve()
    if clean.startswith("/"):
        return Path(clean).resolve()
    _, primary_dir = agent_allowed_roots(reach.home, agent_id, custom_cwd)
    candidates = [
        (primary_dir / clean).resolve(),
        (workspace_path(reach.home, agent_id).resolve() / clean).resolve(),
    ]
    return next((path for path in candidates if path.exists()), candidates[0])


def _reach_workspaces(
    reach: FileReach, agent_id: int, raw_path: str, custom_cwd: str | None, *, allow_root: bool
) -> tuple[Path, list[Path]]:
    explicit = parse_explicit_workspace_path(raw_path, home=reach.home, allow_root=allow_root)
    if explicit is None:
        roots, primary_dir = agent_allowed_roots(reach.home, agent_id, custom_cwd)
        return resolve_workspace_file_path(
            roots, primary_dir, raw_path, allow_root=allow_root
        ), roots
    if explicit[0] != agent_id:
        raise WorkspaceFileError(403, "Access denied: path is outside the agent workspace and cwd")
    ws_root = workspace_path(reach.home, agent_id).resolve()
    target = _resolve_relative_path(explicit[1] or ".", [ws_root], ws_root, allow_root=allow_root)
    return target, [ws_root]


# meiva:note
# The resolved path and the roots it may not leave: None in `all` reach, where only the
# filesystem bounds it.
def _reach(
    reach: FileReach, agent_id: int, raw_path: str, custom_cwd: str | None, *, allow_root: bool
) -> tuple[Path, list[Path] | None]:
    if reach.access == "workspaces":
        return _reach_workspaces(reach, agent_id, raw_path, custom_cwd, allow_root=allow_root)
    try:
        return _reach_any(reach, agent_id, raw_path, custom_cwd), None
    except (OSError, RuntimeError) as exc:
        raise WorkspaceFileError(404, f"Path {raw_path!r} not found") from exc


# meiva:note
# Where the path bar starts for `target`: the agent workspace it is inside, else the account home,
# else the filesystem root.
def file_root(target: Path, reach: FileReach) -> FileRoot:
    workspaces = workspaces_path(reach.home).resolve()
    if target.is_relative_to(workspaces) and target != workspaces:
        owner = target.relative_to(workspaces).parts[0]
        if owner.isdigit() and not owner.startswith("0"):
            return FileRoot(kind="workspace", path=str(workspaces / owner), agent_id=int(owner))
    user_home = reach.user_home.resolve()
    if target.is_relative_to(user_home):
        return FileRoot(kind="home", path=str(user_home), agent_id=None)
    return FileRoot(kind="machine", path=target.anchor, agent_id=None)


# meiva:note
# A regular file the account can read; anything else (a directory, socket, FIFO or device) is
# not a file to serve, in either reach.
def resolve_existing_workspace_file(
    reach: FileReach,
    agent_id: int,
    raw_path: str,
    *,
    custom_cwd: str | None = None,
) -> Path:
    target, _ = _reach(reach, agent_id, raw_path, custom_cwd, allow_root=False)
    if not target.is_file():
        raise WorkspaceFileError(404, f"File {raw_path!r} not found")
    if not os.access(target, os.R_OK):
        raise WorkspaceFileError(403, f"Permission denied: {raw_path!r}")
    return target


def _read_file_payload(
    target: Path,
    ext: str,
    size: int,
    mime: str | None,
) -> tuple[str, bool]:
    is_image = (mime is not None and mime.startswith("image/")) or ext in _IMAGE_EXTENSIONS
    is_media = (
        mime is not None and (mime.startswith(("audio/", "video/")) or mime == "application/pdf")
    ) or ext in _MEDIA_EXTENSIONS
    if is_image or is_media:
        return "", True

    if size > MAX_WORKSPACE_FILE_BYTES:
        raise WorkspaceFileError(413, "File exceeds the 1MB size limit")

    raw_bytes = target.read_bytes()
    try:
        return raw_bytes.decode("utf-8"), False
    except UnicodeDecodeError:
        return "", True


def read_workspace_file_content(
    reach: FileReach,
    agent_id: int,
    raw_path: str,
    *,
    custom_cwd: str | None = None,
) -> WorkspaceFileResponse:
    target = resolve_existing_workspace_file(reach, agent_id, raw_path, custom_cwd=custom_cwd)
    stat = target.stat()
    ext = target.suffix.lower()
    mime, _ = mimetypes.guess_type(target.name)
    content, binary = _read_file_payload(target, ext, stat.st_size, mime)
    return WorkspaceFileResponse(
        path=str(target),
        root=file_root(target, reach),
        name=target.name,
        extension=ext,
        content=content,
        size=stat.st_size,
        binary=binary,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )


def read_workspace_file_raw(
    reach: FileReach,
    agent_id: int,
    raw_path: str,
    *,
    custom_cwd: str | None = None,
    max_bytes: int,
) -> bytes:
    target = resolve_existing_workspace_file(reach, agent_id, raw_path, custom_cwd=custom_cwd)
    stat = target.stat()
    if stat.st_size > max_bytes:
        raise WorkspaceFileError(
            413, f"Referenced file exceeds the {max_bytes}-byte resource limit"
        )
    raw = target.read_bytes()
    if len(raw) > max_bytes:
        raise WorkspaceFileError(
            413, f"Referenced file exceeds the {max_bytes}-byte resource limit"
        )
    return raw


# meiva:note
# A listing answers at most 1,000 entries. The tree renders one row per entry and a forwarded
# listing travels back from the owning runner as a single JSON answer, so a directory like
# `node_modules` must not cost the panel tens of thousands of rows. Only the first 20,000 names a
# directory yields are read, which bounds the sort that picks those 1,000 as well.
MAX_LISTING_ENTRIES = 1000
MAX_LISTING_SCAN = 20_000
type EntryKind = Literal["dir", "file"]

# meiva:note
# One forwarded `Range` answer carries at most 8 MiB: a media element asks `bytes=0-` and then
# follows the `Content-Range` it receives, so a bounded slice starts playback and seeking without
# the runner reading or the gateway relaying a whole file per request.
MAX_RANGE_ANSWER_BYTES = 8 * 1024 * 1024


def resolve_workspace_dir(
    reach: FileReach,
    agent_id: int,
    raw_path: str,
    *,
    custom_cwd: str | None = None,
) -> tuple[Path, list[Path] | None]:
    if raw_path:
        target, roots = _reach(reach, agent_id, raw_path, custom_cwd, allow_root=True)
    else:
        target = workspace_path(reach.home, agent_id).resolve()
        roots = (
            None
            if reach.access == "all"
            else agent_allowed_roots(reach.home, agent_id, custom_cwd)[0]
        )
    if not target.is_dir():
        raise WorkspaceFileError(404, f"Directory {raw_path or '.'!r} not found")
    return target, roots


def _inside_roots(path: Path, roots: list[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


# meiva:note
# Under bounding roots a symlinked child is listed only when its target stays inside one, the
# same rule a read of it would apply; sockets, FIFOs and dangling links are never listed.
def _listed_kind(item: os.DirEntry[str], roots: list[Path] | None) -> EntryKind | None:
    try:
        if (
            roots is not None
            and item.is_symlink()
            and not _inside_roots(Path(item.path).resolve(), roots)
        ):
            return None
        if item.is_dir():
            return "dir"
        if item.is_file():
            return "file"
    except (OSError, RuntimeError):
        return None
    return None


def _listed_entry(directory: Path, name: str, kind: EntryKind) -> WorkspaceDirEntry | None:
    path = directory / name
    try:
        stat = path.stat()
    except OSError:
        return None
    return WorkspaceDirEntry(
        name=name,
        path=str(path),
        kind=kind,
        size=stat.st_size if kind == "file" else None,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )


def list_workspace_dir(
    reach: FileReach,
    agent_id: int,
    raw_path: str,
    *,
    custom_cwd: str | None = None,
    max_entries: int = MAX_LISTING_ENTRIES,
) -> WorkspaceDirListing:
    target, roots = resolve_workspace_dir(reach, agent_id, raw_path, custom_cwd=custom_cwd)
    try:
        with os.scandir(target) as scan:
            items = list(itertools.islice(scan, MAX_LISTING_SCAN + 1))
    except PermissionError as exc:
        raise WorkspaceFileError(403, f"Permission denied: {raw_path or '.'!r}") from exc
    except OSError as exc:
        raise WorkspaceFileError(422, "Workspace directory could not be read") from exc
    truncated = len(items) > MAX_LISTING_SCAN
    kinds: list[tuple[str, EntryKind]] = [
        (item.name, kind)
        for item in items[:MAX_LISTING_SCAN]
        if (kind := _listed_kind(item, roots)) is not None
    ]
    kinds.sort(key=lambda pair: (pair[1] != "dir", pair[0].casefold(), pair[0]))
    entries = [
        entry
        for name, kind in kinds[:max_entries]
        if (entry := _listed_entry(target, name, kind)) is not None
    ]
    parent = target.parent
    climbs = parent != target and (roots is None or _inside_roots(parent, roots))
    return WorkspaceDirListing(
        path=str(target),
        root=file_root(target, reach),
        parent=str(parent) if climbs else None,
        entries=entries,
        truncated=truncated or len(kinds) > max_entries,
    )


# meiva:note
# One byte range of a workspace file: `206` with its slice, or `416` with no body when the range
# starts past the end. `content_range` is the `Content-Range` header value either way.
@dataclass(frozen=True)
class ByteRangeAnswer:
    status: int
    content: bytes
    content_range: str


# meiva:note
# A `bytes=` range that starts at or past the end of the file: answered `416`.
class _RangeNotSatisfiableError(Exception):
    pass


# meiva:note
# One `bytes=` range as inclusive offsets clamped to the file. Anything else (another unit, a
# list of ranges, a malformed spec) answers None, and the caller serves the whole file as HTTP
# lets a server ignore a Range it does not support.
def _parse_byte_range(header: str, size: int) -> tuple[int, int] | None:
    unit, _, spec = header.strip().partition("=")
    first, dash, last = spec.strip().partition("-")
    first, last = first.strip(), last.strip()
    if unit.strip().lower() != "bytes" or "," in spec or not dash:
        return None
    if not first:
        return _suffix_range(last, size)
    if not first.isdigit() or (last and not last.isdigit()):
        return None
    start = int(first)
    if last and int(last) < start:
        return None
    if start >= size:
        raise _RangeNotSatisfiableError
    return start, min(int(last), size - 1) if last else size - 1


# meiva:note
# `bytes=-N`: the last N bytes, the whole file when N exceeds it.
def _suffix_range(last: str, size: int) -> tuple[int, int] | None:
    if not last.isdigit():
        return None
    if int(last) == 0 or size == 0:
        raise _RangeNotSatisfiableError
    return max(size - int(last), 0), size - 1


def read_workspace_file_range(
    reach: FileReach,
    agent_id: int,
    raw_path: str,
    *,
    byte_range: str,
    max_bytes: int,
    custom_cwd: str | None = None,
) -> ByteRangeAnswer | None:
    target = resolve_existing_workspace_file(reach, agent_id, raw_path, custom_cwd=custom_cwd)
    size = target.stat().st_size
    try:
        span = _parse_byte_range(byte_range, size)
    except _RangeNotSatisfiableError:
        return ByteRangeAnswer(status=416, content=b"", content_range=f"bytes */{size}")
    if span is None:
        return None
    start = span[0]
    length = min(span[1] - start + 1, max_bytes, MAX_RANGE_ANSWER_BYTES)
    with target.open("rb") as handle:
        _ = handle.seek(start)
        content = handle.read(length)
    if not content:
        return ByteRangeAnswer(status=416, content=b"", content_range=f"bytes */{size}")
    return ByteRangeAnswer(
        status=206,
        content=content,
        content_range=f"bytes {start}-{start + len(content) - 1}/{size}",
    )


def _json(payload: BaseModel) -> RouteAnswer:
    return 200, payload.model_dump_json().encode("utf-8"), "application/json"


# meiva:note
# The runner's answer to one forwarded workspace read, as the ops listener sends it, under the
# reach the gateway sent; a refusal raises `WorkspaceFileError`. A `raw` read with a `byte_range`
# answers that slice (`206`/`416` with its `Content-Range`) unless the range is one HTTP lets a
# server ignore, which falls through to the whole file under the resource cap.
def answer_workspace_file(
    request: WorkspaceFileRequest, *, home: Path, user_home: Path, limits: ToUserLimits
) -> RouteAnswer:
    reach = FileReach(home=home, user_home=user_home, access=request.file_access)
    custom_cwd = request.custom_cwd
    if request.mode == "content":
        return _json(
            read_workspace_file_content(
                reach, request.agent_id, request.path, custom_cwd=custom_cwd
            )
        )
    if request.mode == "list":
        return _json(
            list_workspace_dir(reach, request.agent_id, request.path, custom_cwd=custom_cwd)
        )
    if request.byte_range is not None:
        answer = read_workspace_file_range(
            reach,
            request.agent_id,
            request.path,
            byte_range=request.byte_range,
            max_bytes=limits.resource_bytes,
            custom_cwd=custom_cwd,
        )
        if answer is not None:
            return (
                answer.status,
                answer.content,
                "application/octet-stream",
                {"Content-Range": answer.content_range},
            )
    raw = read_workspace_file_raw(
        reach,
        request.agent_id,
        request.path,
        custom_cwd=custom_cwd,
        max_bytes=limits.resource_bytes,
    )
    return 200, raw, "application/octet-stream"
