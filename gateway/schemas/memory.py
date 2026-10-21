"""Memory search and graph HTTP contracts."""

from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

from base.api_contracts.memory_search import (
    MEMORY_SEARCH_MAX_EXCLUDE,
    MEMORY_SEARCH_MAX_PATH_CHARS,
    MemorySearchPurpose,
    validate_pool_path,
)

ExcludedPath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MEMORY_SEARCH_MAX_PATH_CHARS),
    AfterValidator(validate_pool_path),
]


class MemorySearchRequest(BaseModel):
    """POST /api/memory/search request — semantic search query."""

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] = Field(
        ..., description="natural-language query"
    )
    k: int = Field(
        default=5,
        ge=1,
        le=100,
        description=(
            "most results wanted; never more than the candidates the purpose ranks "
            "(`memory_rerank_candidates`, `memory_dedup_candidates` for 'dedup')"
        ),
    )
    purpose: MemorySearchPurpose = Field(
        ...,
        description=(
            "why the query was issued: 'recall' (passive recall; the query is the recent "
            "conversation), 'search' (an agent's own search) or 'dedup' (the duplicate check on "
            "a note an agent just wrote; the query is that note); selects the ranking question"
        ),
    )
    exclude: list[ExcludedPath] = Field(
        default_factory=list,
        max_length=MEMORY_SEARCH_MAX_EXCLUDE,
        description=(
            "memory-pool-relative note paths to leave out: they are dropped from the vector "
            "candidates before ranking, so they neither cost a ranking request nor take a "
            "candidate slot"
        ),
    )
    deadline_seconds: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
        description=(
            "how long the caller waits, in seconds: the search stops, and the ranking requests "
            "still running are cancelled, when this passes. Can only shorten the gateway's own "
            "search deadline. Omitted: the gateway's deadline applies"
        ),
    )


class MemorySearchResultItem(BaseModel):
    """A single search result: file path, the chunk that matched, its relevance
    score, and the frontmatter a caller can judge the hit by without opening it."""

    path: str = Field(..., description="relative path from memory pool root")
    description: str = Field(default="", description="frontmatter description field, or empty")
    tags: list[str] = Field(
        default_factory=list,
        description="frontmatter tags, including the note's `type/<x>`, or empty",
    )
    score: float = Field(
        ..., ge=0, le=1, description="probability that the matched chunk is relevant to the query"
    )
    chunk: str = Field(
        ...,
        description="text of the note's best-matching chunk (or its description line), as indexed",
    )


class MemorySearchResponse(BaseModel):
    """POST /api/memory/search response — RELATIVE paths from memory pool root.

    Caller (SDK `meiva.memory.search`) uses `meiva.memory.PATH / p` to
    reconstruct absolute paths. fs-neutral makes mismatched gateway
    (e.g. /Users/x) and agent-runner (/home/y) filesystems work.

    `results` carries path, description, tags, score and matched chunk for each
    match, most relevant first.
    """

    results: list[MemorySearchResultItem]


class MemoryRefreshResponse(BaseModel):
    """POST /api/memory/refresh response."""

    head: str  # HEAD commit sha of the gateway memory checkout after fast-forward to origin/main


# meiva:note
# GET /api/memory/note carries the parsed fields plus the markdown body with its YAML
# frontmatter already stripped, so a reader renders the note instead of re-parsing it.
# meiva:link gateway/routers/docs/memory.meiva.okf.md
class MemoryNoteResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    title: str
    description: str | None
    tags: list[str]
    timestamp: str | None
    meiva_agent: str | None
    meiva_machine: str | None
    body: str
