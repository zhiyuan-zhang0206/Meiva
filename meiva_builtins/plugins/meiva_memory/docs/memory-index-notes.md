# Memory index notes

Both stores put an index in front of the agent at the start of every context
window, and only the index — each memory is one file beside it, read on demand.
`notes.py` defines the two builders; `agent_runtime.py` registers the three
context notes and the `memory_discipline_section` prompt section. `plugin.py`
declares the `meiva.memory` SDK surface. `init_context` lays down the ordered
notes whenever a window is established. The third memory note, the
ancestors' `memory/INHERIT.md` files read off the birth chain, is
[Memory inheritance — INHERIT.md read off the birth chain](memory-inheritance.md).

Neither note is core. Disabling `meiva_memory` removes the stores, the SDK surface
and these notes together — the agent is then not told about a capability it does
not have. The write discipline both stores share is a system-prompt section
(`agent_runtime.py`), not a note: it is fixed text, where an index is not. Ranks, fork
behavior and the window-side wiring are
[Memory Injection](../../../../agent/graph/docs/context-notes/memory-injection.md)'s
to state.

## The shared index

`memory_index_note` renders the pool's `MEMORY.md` at `meiva.memory.PATH` as one
system note, behind a framing that states location and mechanics only. The text
is scanned with `scan_content` before injection (audit round-2 up-security-trust
P0-2); when flagged, the note keeps the content and prefixes the visible security
warning. The note returns None
when the layer is off (or eval-isolated) and when the index file is missing or
empty.

## The per-agent index

`per_agent_memory_note` returns a note when enabled and the agent id is
established: the agent's own index
(`workspace_dir(agent_id)` + `memory/MEMORY.md`), creating the directory and an
empty index when missing, and rendering the framing plus `(no content)` when the
index is empty. Entry files beside the index are not injected — the agent reads
them on demand. The framing names location and mechanics only and must not claim how
relative paths resolve — the old claim misled agents into writing memory beside
the tracked cwd (audit #577); that statement belongs to the SDK core.

## The per-agent soft cap

Above `memory_per_agent_index_max_lines` a maintenance nudge is appended below
the index — the index is still injected in full; the note only nudges, it never
truncates. The count is the shared line measure and the judgment is the shared
budget's soft threshold: a cap of 0 or less reads as ok, disabling the check.

## Key Dependencies

- [meiva_memory — Memory Pool Plugin](meiva_memory.md) — the owning plugin
- [Memory Injection — The Three Memory Notes Behind the System Prompt](../../../../agent/graph/docs/context-notes/memory-injection.md) — the notes in the window: ranks, switches, fork behavior
- [Text length budgets](../../../../base/packages/docs/text-budget.md) — the shared metric and budget component
