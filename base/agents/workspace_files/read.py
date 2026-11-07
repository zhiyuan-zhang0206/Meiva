import mimetypes
from datetime import UTC, datetime
from pathlib import Path

from base.agents.workspace_files.errors import (
    WorkspaceFileFailure,
    WorkspaceFileRefused,
    WorkspaceFileTooLarge,
)
from base.agents.workspace_files.models import WorkspaceFile, WorkspaceFileBytes
from base.agents.workspace_files.paths import allowed_roots, resolve_workspace_file_path

# meiva:note
# A file whose bytes are not text, decided before any read: the panel renders these through the raw
# route, so decoding them here would only produce a refusal the viewer already knows how to avoid.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"})
_MEDIA_EXTENSIONS = frozenset(
    {".mp4", ".webm", ".mov", ".mkv", ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".pdf"}
)


# meiva:note
# Resolve the path and prove the file is there before anything reads it, so "missing" is answered
# without a second traversal and a directory is never served as a file.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
def locate_workspace_file(
    home: Path,
    agent_id: int,
    *,
    custom_cwd: str | None,
    raw_path: str,
) -> tuple[Path, list[Path]]:
    roots, primary = allowed_roots(home, agent_id, custom_cwd)
    target = resolve_workspace_file_path(roots, primary, raw_path)
    if not target.exists() or not target.is_file():
        raise WorkspaceFileRefused(WorkspaceFileFailure.NOT_FOUND, f"File {raw_path!r} not found")
    return target, roots


# meiva:note
# The path a reader sees: relative to the root the file was found under, so the panel never learns
# where on disk the agent's home is, and a file outside every root falls back to its own name.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
def relative_display_path(target: Path, roots: list[Path]) -> str:
    for root in roots:
        if target.is_relative_to(root):
            return str(target.relative_to(root)).replace("\\", "/")
    return target.name


# meiva:note
# One text read: the size budget is checked before the bytes are pulled, and a file that is not
# valid UTF-8 is reported as binary rather than refused, because the viewer has a route for that.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
def read_workspace_file(
    home: Path,
    agent_id: int,
    *,
    custom_cwd: str | None,
    raw_path: str,
    max_bytes: int,
) -> WorkspaceFile:
    target, roots = locate_workspace_file(home, agent_id, custom_cwd=custom_cwd, raw_path=raw_path)
    stat = target.stat()
    extension = target.suffix.lower()
    mime, _ = mimetypes.guess_type(target.name)
    is_media = (
        (mime is not None and mime.startswith(("image/", "audio/", "video/")))
        or mime == "application/pdf"
        or extension in _IMAGE_EXTENSIONS
        or extension in _MEDIA_EXTENSIONS
    )
    content = ""
    binary = is_media
    if not is_media:
        if stat.st_size > max_bytes:
            raise WorkspaceFileTooLarge(stat.st_size, max_bytes)
        raw = target.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            binary = True
    return WorkspaceFile(
        path=relative_display_path(target, roots),
        name=target.name,
        extension=extension,
        content=content,
        size=stat.st_size,
        binary=binary,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )


# meiva:note
# One byte read: no decode and no extension judgement, because the caller asked for the file the
# viewer renders, and `max_bytes` is the transport's own ceiling rather than a rendering one.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
def read_workspace_file_bytes(
    home: Path,
    agent_id: int,
    *,
    custom_cwd: str | None,
    raw_path: str,
    max_bytes: int | None = None,
) -> WorkspaceFileBytes:
    target, _ = locate_workspace_file(home, agent_id, custom_cwd=custom_cwd, raw_path=raw_path)
    size = target.stat().st_size
    if max_bytes is not None and size > max_bytes:
        raise WorkspaceFileTooLarge(size, max_bytes)
    return WorkspaceFileBytes(name=target.name, content=target.read_bytes())
