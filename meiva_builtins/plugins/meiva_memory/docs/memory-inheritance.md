# Memory inheritance

An ancestor shares standing content with its descendants by writing it as pure
Markdown into `<workspace>/memory/INHERIT.md` — no fences or special tags. Every
descendant within `memory_inherit_depth` hops up the immutable birth chain
(`agents_meta.born_spawner`, nearest first) receives the body of each ancestor's
`INHERIT.md` as a context note when its window is established — cold start, after
a compaction, or on a fork (the fork keeps the inherited copy in the source's
history and grafts its own chain's after the fork marker). Only `INHERIT.md` is
injected; optional detail files under `memory/inherit/<slug>.md` stay on disk for
descendants to read on demand.
The chain read is one light gateway round-trip (no tie graph), cached per
process: `born_spawner` is append-only, so a row's chain never changes. Entry
files are read off THIS machine's workspaces — an ancestor on another machine
is skipped and named in the footer (same-machine read, v1). Ranks, switches and
the window-side wiring are
[Memory Injection](../../../../agent/graph/docs/context-notes/memory-injection.md)'s
to state.

## The entry body

`extract_inherit_body` strips CRLF, an optional leading YAML frontmatter block
(skipped via the same split the pool validator uses) and surrounding whitespace;
what remains is the ancestor's section. A missing, unreadable or empty file
contributes nothing.

## The chain read

Read once per establishment; a failed read (or a host without a machine name)
degrades to no note with a warning and is not cached. Only ancestors whose
`agents_meta.machine` is this host contribute; an ancestor with no local
`INHERIT.md` is skipped quietly.

## Size guardrails

One oversized body must not balloon every descendant's context: the per-block
cap (`memory_inherit_max_block_chars`) applies first, then the total budget
(`memory_inherit_max_total_chars`) clips the crossing section to its remainder
and omits the rest behind one marker; `0` disables either. Truncation leaves
visible markers and a warning; the lengths are the shared character measure.

## The note

One section per ancestor — nearest ancestor first — behind the framing, with no
timestamps, so unchanged state renders byte-identical (the property the fork's
prefix-cache stability relies on). The assembled text is injected as it is. The note is None when
the layer is off (depth 0 or eval isolation), no agent id is established, the
chain read failed, or no ancestor declared anything.

## Where the decisions live

`inheritance/inherited_blocks.py` is the strict pure half: which birth-chain rows this host can read
(`split_ancestors`), the body of one entry (`extract_inherit_body`) and the note
text with both size caps applied (`render_inherited`). It takes plain values and returns
values, warnings included, so it logs nothing and reads nothing. `inheritance/inherit.py` is the thin
shell: it reads the agent's slices, makes the one gateway call, reads the `INHERIT.md` files of the
local ancestors, logs the returned warnings and wraps the text into the note. The
tests run the pure half on literal values and the shell against a gateway on a port-0
listener and entry files under a unit home.

## Key Dependencies

- [meiva_memory — Memory Pool Plugin](meiva_memory.md) — the owning plugin
- [Memory Injection — The Three Memory Notes Behind the System Prompt](../../../../agent/graph/docs/context-notes/memory-injection.md) — the notes in the window: ranks, switches, fork behavior
- [Text length budgets](../../../../base/packages/docs/text-budget.md) — the shared metric and budget component
