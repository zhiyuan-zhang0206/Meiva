from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

DOCUMENT_FILENAME = "to_user.md"
ONGOING_FILENAME = "ongoing.md"

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


class OngoingDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str | None
    version: str | None
    modified_at: datetime | None


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
# One read the gateway forwards to the machine that owns the path. `list` reads an empty `path`
# as the workspace root; `byte_range` (the browser's `Range` header, verbatim) belongs to `raw`;
# `file_access` is the gateway's reach setting, so the owning runner applies the gateway's
# decision. The gateway leaves default fields off the wire, so a runner that predates a field
# still accepts the reads it already knew.
# meiva:link gateway/agents/docs/to-user-resources.md
class WorkspaceFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: int = Field(gt=0)
    path: str = Field(max_length=MAX_FILE_LENGTH)
    mode: Literal["content", "raw", "list"]
    custom_cwd: str | None = None
    byte_range: str | None = Field(default=None, max_length=256)
    # meiva:note
    # The values of `base.config.domains.to_user.FileAccess`, spelled out: the models stay importable
    # without the config layer (and `pydantic_settings`), which the exec child's lite boot must not load.
    file_access: Literal["all", "workspaces"] = "workspaces"


# meiva:note
# Where the viewer's path bar starts for a path on its owning machine: the agent workspace it is
# inside (`agent_id` names the agent), else the account home, else the filesystem root `/`.
# `path` is that root's absolute directory there.
class FileRoot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["workspace", "home", "machine"]
    path: str
    agent_id: int | None


# meiva:note
# `path` is the file's absolute resolved path on its owning machine.
class WorkspaceFileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    root: FileRoot
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
# `path` is the directory's absolute resolved path on its owning machine; `parent` is null when
# the directory is a root the viewer may not climb out of.
class WorkspaceDirListing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    root: FileRoot
    parent: str | None
    entries: list[WorkspaceDirEntry]
    truncated: bool
