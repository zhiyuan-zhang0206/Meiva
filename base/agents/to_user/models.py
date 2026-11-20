from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

DOCUMENT_FILENAME = "to_user.md"

# meiva:note
# Linux refuses a path longer than PATH_MAX, so no document can name a file with a longer name.
MAX_FILE_LENGTH = 4096


# meiva:note
# The readable name of one referenced file, and the `file` value in the panel's address bar. It
# is only ever compared to the names the document's own references produce, so no pattern can
# make a stranger's path servable; the shape is bounded so a malformed request still fails at
# the boundary instead of travelling to the owning machine.
ResourceFile = Annotated[
    str, Field(min_length=1, max_length=MAX_FILE_LENGTH, pattern=r"^[^\x00]+$")
]


class ToUserResource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: ResourceFile
    name: str
    markdown: bool
    version: str | None


class ToUserDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str | None
    version: str | None
    modified_at: datetime | None
    resources: dict[str, ToUserResource] = Field(default_factory=dict)
    legacy_html: bool = False


class ToUserReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: int = Field(gt=0)
    files: list[ResourceFile] = Field(default_factory=list)


# meiva:note
# One read the gateway forwards to the machine that owns the workspace. `list` reads an empty
# `path` as the workspace root; `byte_range` (the browser's `Range` header, verbatim) belongs to
# `raw`. The gateway sends unset fields as absent, so a runner that predates a field still accepts
# the reads it already knew.
# meiva:link gateway/agents/docs/to-user-resources.md
class WorkspaceFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: int = Field(gt=0)
    path: str = Field(max_length=MAX_FILE_LENGTH)
    mode: Literal["content", "raw", "list"]
    custom_cwd: str | None = None
    byte_range: str | None = Field(default=None, max_length=256)


class WorkspaceFileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    name: str
    extension: str
    content: str
    size: int
    binary: bool
    modified_at: datetime


# meiva:note
# One child of a listed directory; `size` is null for a directory.
class WorkspaceDirEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    path: str
    kind: Literal["dir", "file"]
    size: int | None
    modified_at: datetime


# meiva:note
# `parent` is null when the directory is a root the viewer may not climb out of.
class WorkspaceDirListing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    parent: str | None
    entries: list[WorkspaceDirEntry]
    truncated: bool
