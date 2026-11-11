# meiva.memory — Long-term Memory Pool

## Attribution

**Provided by the `meiva_memory` plugin**. Not a core module — disabling the plugin makes `meiva.memory.PATH`, `meiva.memory.PERSONAL_PATH` and `meiva.memory.search()` entirely unavailable.

## What it is

A shared markdown note folder (`~/.meiva/vault/memory`), used by all agents. Notes are discovered via semantic search and synced across machines within approximately one day.

Two types of memory, each serving different audiences:
- **Shared pool** (`meiva.memory.PATH`): notes visible to all agents. Write durable facts that other agents need when taking over your role — user preferences, hard constraints, long-term decisions.
- **Per-agent memory** (`meiva.memory.PERSONAL_PATH`, `<workspace>/memory/`): your own durable state, maintained by yourself (the agent keeps its `MEMORY.md` index by hand), surviving across compactions.

### Descendant inheritance (personal store → descendants)

An agent hands standing guidance to the descendants it spawns by writing pure Markdown in `INHERIT.md` in its personal memory folder; there are no fences or special tags. Only `INHERIT.md` is injected; detail files the agent keeps under `inherit/<slug>.md` are read on demand by descendants.

- `<workspace>/memory/INHERIT.md` is read at every context-window establishment (cold start / post-compact / fork regraft) for the first `memory_inherit_depth` (default 1, 0=off, ≤10) hops up the birth chain, nearest ancestor first.
- Only ancestors that ran on the same machine contribute; a remote ancestor is skipped and named in the note's footer.
- Size guardrails: `memory_inherit_max_block_chars` (4000) / `memory_inherit_max_total_chars` (16000) — over either, the note carries a visible `[truncated …]` marker and the log a warning; 0 disables one.
- Content is a pure function of (chain, files, settings) — no timestamps — so unchanged state re-renders byte-identical (fork prefix-cache stability).

## Core API

- `PATH: PosixPath` — shared memory pool root directory `~/.meiva/vault/memory`; its `help()` text is the note format an agent follows
- `PERSONAL_PATH: PosixPath` — the agent's own memory folder, `<workspace>/memory`, resolved from the current SDK identity (an attached external lease first, then the process identity) when read; raises when no identity is bound
- `search(query, k=5, *, timeout=None) → list[MemoryMatch]` — semantic search; returns up to `k` `MemoryMatch(path, description, tags, score, chunk)` named tuples, most relevant first. `path` is absolute, `score` is a decision model's probability (0..1) that the matched `chunk` (the note's best passage, or its description line) is relevant to the query, and every match is returned whatever its score — the caller decides what a weak match deserves. `timeout` bounds one attempt (default = the gateway's own search deadline + 3s, 18s); when given it also travels in the request as the search's deadline, so the gateway stops ranking when it passes, and the HTTP timeout is `timeout` + 3s. A congested index answers 503 (`IndexerUnavailable`) in ~1s instead of queueing the call. A search returns at most the notes one search ranks (`memory_rerank_candidates`, 30), whatever `k` is
- `MemoryMatch` — the result tuple above, exported in `__all_for_meiva__` so `help(meiva.memory)` renders its fields
- `IndexerUnavailable` — exception when the indexer service is unavailable, or when the decision model that ranks results fails (search never falls back to unranked vector order)

Search is two stages, both on the gateway: vector search returns the best chunk of the
closest `memory_rerank_candidates` notes (default 30), then the decision model
`memory_rerank_model` answers a yes/no "worth reading for this query" question for each chunk (one request per candidate; `purpose` tells recall from an agent search) and the response is
ordered by the probability of yes. That count is the most decision requests one search issues: neither `k` nor the request's `exclude` list (paths dropped before ranking, used by passive recall; internal, not an `meiva.memory.search` parameter) changes it. No text is generated. The gateway response contains only
`results`, each with a memory-root-relative `path`, frontmatter `description` and `tags`, the
`score` and the matched `chunk`. The SDK resolves each path against the runner's memory root;
the HTTP response has no duplicate bare-path list.

Personal-index injection and `PERSONAL_PATH` resolve the current SDK identity:
an attached external lease takes precedence over a hosted turn, then the process
identity. External lease validity is checked before reading or creating memory
files. A note written with `meiva.files` takes effect on the filesystem immediately;
it is not a plugin state journal entry and does not wait for any handoff.

## Writing a note

There is no writer function: the agent writes the whole note directly in pure Markdown (YAML frontmatter is optional) with `meiva.files.write` / `append` / `edit` under `PATH` or `PERSONAL_PATH`, following the format in `help(meiva.memory.PATH)`. `meiva.files` stores the content as given and nothing is checked at write time: the pool validator (`template/validate.py`) enforces the format at commit and Dream time ([Memory Pool Scaffold and Validator](memory-pool-scaffold.md)). A note is a `# Title`, a short description paragraph and a body, at most 8000 characters; YAML `sources` is forbidden, and provenance uses inline body file links.

## Key Dependencies

- [meiva_memory — Memory Pool Plugin](meiva_memory.md) — the owning plugin
- [Memory-Indexer — Memory Pool Vector Index](../../../../services/docs/gateway_side/memory_indexer/memory-indexer.md) — vector index service
- [Memory Search — NumPy exact-search service](../../../../services/docs/gateway_side/memory_search/memory-search.md) — the vector store service
- [Memory Recall — Passive Memory Recall](memory-recall.md) — passive recall mechanism (provided by the same plugin)
- [Memory Pool Scaffold and Validator](memory-pool-scaffold.md) — the rules a written note is checked against at commit time
