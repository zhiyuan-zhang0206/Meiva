from dataclasses import dataclass
from datetime import datetime


# meiva:note
# One readable workspace file, as both the machine that owns the disk and the gateway that serves
# it describe it: `path` is relative to the root it was found under, so a caller never learns an
# absolute path, and `binary` says whether `content` carries the bytes or the reader must use the
# raw route.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
@dataclass(frozen=True)
class WorkspaceFile:
    path: str
    name: str
    extension: str
    content: str
    size: int
    binary: bool
    modified_at: datetime


# meiva:note
# A file served as bytes: the name is all the HTTP layer needs to rebuild a disposition and a
# media type, and the body travels beside it so no second stat races the read.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
@dataclass(frozen=True)
class WorkspaceFileBytes:
    name: str
    content: bytes
