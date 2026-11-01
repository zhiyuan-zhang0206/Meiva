# meiva:note
# Shared helpers for agent file uploads. An upload moves through two directories, and the
# difference is the point:
#
# - the staging directory (`<MEIVA_HOME>/uploads/<agent_id>`, overridable) is where the gateway
#   keeps the bytes a person picked until they send them (`.staged/<file id>`), then until the
#   agent's unit has a verified copy. Once the send is accepted the staged bytes are deleted;
# - the delivered directory (`<MEIVA_HOME>/workspaces/<agent_id>/<name>/<send time>`) is on the
#   machine that runs the agent, inside that agent's own workspace — the place the agent can
#   read, under the person's own file names.
#
# The staging root is the same on every gateway; the delivered root is the target machine's own
# home (`UnitIdentity.home`), so a copy that crosses machines lands in the receiver's workspace,
# not in a directory on the sender. The flat `meiva-upload-<uuid>-<n><suffix>` files that earlier
# native-image uploads left in staging are only read back (`/api/agents/N/uploads/<name>`), for
# the old messages that name them; nothing writes one any more.

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from base.agents.messages.inbound_images import image_mime_for
from base.paths import workspace_path
from base.paths.private_dir import ensure_private_dir


class UploadResponse(Protocol):
    @property
    def content(self) -> bytes: ...

    def raise_for_status(self) -> object: ...


class UploadClient(Protocol):
    def get(self, url: str, *, timeout: float) -> UploadResponse: ...


def agent_upload_dir(agent_id: int, *, root: Path, create: bool = True) -> Path:
    directory = root / str(agent_id)
    return ensure_private_dir(directory) if create else directory


# meiva:note
# Where a staged file's bytes live inside the agent's staging directory. The name is the
# file's own id, never the person's file name, so no caller-chosen name reaches a path.
STAGED_NAMESPACE = ".staged"


def staged_path(directory: Path, file_id: str) -> Path:
    return directory / STAGED_NAMESPACE / file_id


# meiva:note
# The delivered copy lives in the agent's own workspace, derived from the home of the
# machine that runs it (`UnitIdentity.home`), never from the sender's. `workspace_path` is
# the pure derivation there; this function only adds the per-cluster directory name the
# upload owner is configured with.
def delivered_upload_dir(agent_id: int, *, home: Path, name: str, create: bool = True) -> Path:
    directory = workspace_path(home, agent_id) / name
    return ensure_private_dir(directory) if create else directory


def upload_url(agent_id: int, name: str) -> str:
    """The HTTP path an uploaded file is addressed by."""
    return f"/api/agents/{agent_id}/uploads/{name}"


def delivered_upload_url(agent_id: int, batch_id: str, ordinal: int) -> str:
    return f"/api/agents/{agent_id}/uploads/{batch_id}/{ordinal}"


# meiva:note
# Traversal-safe resolution of an upload name inside one directory: a name that escapes it
# (`..`, an absolute path, a separator that survived) raises `ValueError`, so a crafted url or
# filename cannot reach an arbitrary file.
def resolve_upload_path(directory: Path, name: str) -> Path:
    base = directory.resolve()
    target = (base / name).resolve()
    if target == base or not target.is_relative_to(base):
        raise ValueError(f"upload name {name!r} escapes the upload directory {base}")
    return target


# meiva:note
# Fetch a historical native-image upload from the gateway over HTTP and return
# (mime_type, base64_ascii). Only an inbound queued with an `image_url` block before
# uploads unified onto file delivery still names one; nothing writes such a block now.
# meiva:link base/agents/upload_delivery/docs/storage.meiva.okf.md
def fetch_upload_b64(
    agent_id: int, name: str, *, client: Callable[[], UploadClient]
) -> tuple[str, str]:
    mime = image_mime_for(name)
    if mime is None:
        raise ValueError(f"upload {name!r} is not a recognized image type")
    import httpx

    url = f"/api/agents/{agent_id}/uploads/{name}"
    try:
        resp = client().get(url, timeout=15.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise OSError(f"fetch upload {url!r} failed: {exc}") from exc
    return mime, base64.standard_b64encode(resp.content).decode("ascii")


# ── upload limits (audit round-2 security P1-2) ─────────────────────────────
# The upload endpoint is the gateway's one authenticated user->disk write
# surface. Without caps it is an unlimited write-to-disk DoS (any caller with
# a session/Bearer can fill the disk) and its serve-back path can hand a
# browser a rendered HTML/SVG payload (stored XSS). These constants bound the
# write; `serve_upload` headers bound the serve-back. Deliberately constants,
# not config: a limit that can be tuned away per-cluster is a limit that
# silently disappears; operators who need more change the constant.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # per file — read-into-memory + disk cap
MAX_AGENT_UPLOAD_BYTES = 500 * 1024 * 1024  # total per agent (quota)
MAX_AGENT_UPLOAD_FILES = 1000  # file count per agent (inode-ish bound)
STAGED_FILE_TTL_SECONDS = 24 * 60 * 60

# Suffixes whose content executes in a browser when served inline (direct
# navigation / iframe). Served with Content-Disposition: attachment +
# X-Content-Type-Options: nosniff instead of refused, so a legitimate upload
# (an HTML mockup an agent is asked to analyze) still works — the agent reads
# files itself and is unaffected by how the browser would render them.
_RENDERABLE_SUFFIXES = frozenset({".html", ".htm", ".svg", ".xhtml", ".xml", ".md"})


def render_safe_headers(name: str) -> dict[str, str]:
    """Extra response headers for serving an upload back. Renderable types get
    attachment + nosniff so a stored payload cannot execute in a browser;
    everything else gets nosniff alone (belt and suspenders — a content-type
    mismatch is then inert)."""
    headers = {"X-Content-Type-Options": "nosniff"}
    if Path(name).suffix.lower() in _RENDERABLE_SUFFIXES:
        headers["Content-Disposition"] = f'attachment; filename="{Path(name).name}"'
    return headers


_INLINE_RASTER_SUFFIXES: dict[str, tuple[str, ...]] = {
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
    "image/gif": (".gif",),
    "image/webp": (".webp",),
    "image/avif": (".avif",),
    "image/bmp": (".bmp",),
}


def delivered_upload_headers(name: str, content_type: str, sha256: str) -> dict[str, str]:
    media = content_type.split(";", 1)[0].strip().lower()
    suffix = Path(name).suffix.lower()
    allowed_suffixes = _INLINE_RASTER_SUFFIXES.get(media, ())
    disposition = "inline" if suffix in allowed_suffixes else "attachment"
    return {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name, safe='')}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "Cache-Control": "private, max-age=31536000, immutable",
        "ETag": f'"{sha256}"',
    }
