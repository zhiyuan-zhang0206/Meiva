import contextlib
import mimetypes
from datetime import UTC, datetime
from pathlib import Path

from psycopg_pool import ConnectionPool

from base.agents.to_user import (
    ToUserDocument,
    ToUserLimits,
    WorkspaceFileResponse,
    read_document,
    read_failure,
)
from base.paths import workspace_path

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
# meiva:link gateway/agents/docs/to-user-read-failures.meiva.okf.md
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


def clean_file_path(raw_path: str) -> str:
    clean = raw_path.split("#", 1)[0].split("?", 1)[0].strip()
    if not clean:
        raise WorkspaceFileError(400, "Path parameter cannot be empty")
    if clean.startswith("file://"):
        clean = clean[len("file://") :]
    if "\0" in clean:
        raise WorkspaceFileError(400, "Invalid path: null bytes not allowed")
    return clean


def _resolve_absolute_path(
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

    raise WorkspaceFileError(403, "Access denied: path is outside the agent workspace and cwd")


def _resolve_relative_path(
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

    raise WorkspaceFileError(403, "Access denied: path is outside the agent workspace and cwd")


def resolve_workspace_file_path(
    allowed_roots: list[Path],
    primary_dir: Path,
    raw_path: str,
) -> Path:
    clean = clean_file_path(raw_path)
    clean_p = Path(clean)
    if clean_p.is_absolute():
        return _resolve_absolute_path(clean_p, clean, allowed_roots, primary_dir)
    return _resolve_relative_path(clean, allowed_roots, primary_dir)


def resolve_existing_workspace_file(
    home: Path,
    agent_id: int,
    raw_path: str,
    *,
    custom_cwd: str | None = None,
) -> tuple[Path, list[Path]]:
    allowed_roots, primary_dir = agent_allowed_roots(home, agent_id, custom_cwd)
    target = resolve_workspace_file_path(allowed_roots, primary_dir, raw_path)
    if not target.exists() or not target.is_file():
        raise WorkspaceFileError(404, f"File {raw_path!r} not found")
    return target, allowed_roots


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


def _relative_display_path(target: Path, allowed_roots: list[Path]) -> str:
    for root in allowed_roots:
        if target.is_relative_to(root):
            return str(target.relative_to(root)).replace("\\", "/")
    return target.name


def read_workspace_file_content(
    home: Path,
    agent_id: int,
    raw_path: str,
    *,
    custom_cwd: str | None = None,
) -> WorkspaceFileResponse:
    target, allowed_roots = resolve_existing_workspace_file(
        home, agent_id, raw_path, custom_cwd=custom_cwd
    )
    stat = target.stat()
    ext = target.suffix.lower()
    mime, _ = mimetypes.guess_type(target.name)
    content, binary = _read_file_payload(target, ext, stat.st_size, mime)
    rel_path = _relative_display_path(target, allowed_roots)
    return WorkspaceFileResponse(
        path=rel_path,
        name=target.name,
        extension=ext,
        content=content,
        size=stat.st_size,
        binary=binary,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )


def read_workspace_file_raw(
    home: Path,
    agent_id: int,
    raw_path: str,
    *,
    custom_cwd: str | None = None,
    max_bytes: int,
) -> bytes:
    target, _ = resolve_existing_workspace_file(home, agent_id, raw_path, custom_cwd=custom_cwd)
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
