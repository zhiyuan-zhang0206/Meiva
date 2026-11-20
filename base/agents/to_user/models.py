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
# The longest quick-open query, and how many results one search returns by default and at most.
MAX_SEARCH_QUERY_LENGTH = 256
DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 200


# meiva:note
# One read the gateway forwards to the machine that owns the workspace. `list` and `search` read
# an empty `path` as the workspace root; `query`/`limit` belong to `search` and `byte_range` (the
# browser's `Range` header, verbatim) to `raw`. The gateway sends unset fields as absent, so a
# runner that predates a field still accepts the reads it already knew.
# meiva:link gateway/agents/docs/to-user-resources.md
class WorkspaceFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: int = Field(gt=0)
    path: str = Field(max_length=MAX_FILE_LENGTH)
    mode: Literal["content", "raw", "list", "search"]
    custom_cwd: str | None = None
    query: str | None = Field(default=None, min_length=1, max_length=MAX_SEARCH_QUERY_LENGTH)
    limit: int | None = Field(default=None, ge=1, le=MAX_SEARCH_LIMIT)
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


class WorkspaceSearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    name: str
    relative: str


# meiva:note
# `truncated` says matches may be missing: the scan stopped at its bound, or more files matched
# than the limit returned.
class WorkspaceFileSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    results: list[WorkspaceSearchHit]
    truncated: bool
