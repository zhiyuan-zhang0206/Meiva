from enum import StrEnum


# meiva:note
# What a reader of an agent's workspace file can do next, and only that difference earns a class:
# `outside_roots` and `not_found` both end the request, while `too_large` ends when the file
# shrinks and `path_invalid` when the caller sends a path the resolver can read at all.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
class WorkspaceFileFailure(StrEnum):
    PATH_INVALID = "path_invalid"
    OUTSIDE_ROOTS = "outside_roots"
    NOT_FOUND = "not_found"
    TOO_LARGE = "too_large"


# meiva:note
# The refusal carries its class and the path the caller asked for, so an HTTP adapter can answer
# with a status and a detail without re-deriving why the read stopped. It is a ValueError because
# every caller up to now classified a refused workspace read that way.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
class WorkspaceFileRefused(ValueError):  # noqa: N818
    def __init__(self, failure: WorkspaceFileFailure, detail: str) -> None:
        super().__init__(detail)
        self.failure = failure
        self.detail = detail


# meiva:note
# The byte budget is the one refusal a reader distinguishes by type rather than by message: every
# other class comes from the filesystem or the resolver, which name themselves.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
class WorkspaceFileTooLarge(WorkspaceFileRefused):
    def __init__(self, size: int, limit: int) -> None:
        super().__init__(
            WorkspaceFileFailure.TOO_LARGE, f"File is {size} bytes; the limit is {limit}"
        )
