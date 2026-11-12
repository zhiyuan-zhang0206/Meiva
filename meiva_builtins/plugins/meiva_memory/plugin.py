"""meiva_memory plugin — shared memory pool: the full `meiva.memory` surface + passive recall hook + daily consolidation skill.

Three capabilities live under one plugin because they share the same domain (the
shared memory pool at meiva.memory.SHARED_PATH):

1. **meiva.memory namespace** (PATH + PERSONAL_PATH + search) — the agent-facing SDK surface.
   This plugin OWNS the `meiva.memory` top-level namespace. Disabling it removes
   `meiva.memory` entirely — PATH, PERSONAL_PATH, search(), IndexerUnavailable — not just
   passive recall. There is no writer: an agent writes a note with `meiva.files.write` under
   PATH or PERSONAL_PATH.

2. **Passive memory recall** (before_llm hook) — on a turn woken by fresh user
   inbound, searches the pool semantically on the recent conversation and
   injects the top matching note references so relevant durable notes surface
   without the agent asking. The recall engine lives in
   agent/graph/recall/memory_recall.py; this plugin is the wiring plus its firing
   gates.

3. **Memory stewardship + daily consolidation** (bundled skills) — the pool is
   a git repo. Once a day the day's notes are consolidated (Dream-time OKF
   enrichment, `validate.py`, index rebuild), committed, pushed, and re-indexed
   so search stays current. The procedure varies by deployment: single-box runs
   `skills/scripts/consolidate.py`; multi-host spawns one steward per machine, then
   the arbiter merges all PRs. The stewardship playbook (role, health checks,
   note merging, query service) is at meiva.skills.meiva_memory; the CLI-backed
   consolidation procedure is the meiva.skills.meiva_memory.consolidation
   sub-skill.

Disabling this plugin removes the entire meiva.memory surface, passive recall,
and the bundled memory skills.

Passive recall in detail:

Where the standing memory index keeps MEMORY.md permanently in front of the
agent, passive recall reaches into the *rest* of the pool: on a turn woken by
fresh inbound, it runs a semantic search keyed on the recent conversation
and injects the top matches (path + frontmatter description, the same fields
meiva.memory.search returns) as a system-styled note. The agent sees durable
notes relevant to what is being said without having to call meiva.memory.search
itself.

The heavy lifting (query build, search, dedup vs already-injected paths, note
rendering) lives in agent/graph/recall/memory_recall.py; this plugin is just the
before_llm wiring plus its firing gates.

Firing gates:
- Feature gate: no-op unless `passive_memory_recall_enabled`.
- Trigger gate: fire when the message tail carries fresh inbound from a real
  source — user chat, a peer agent (`agent:`), a scheduled turn (`schedule:`),
  or a system notice — and skip machine-originated wake-ups (`watcher:` /
  `shell:` prefixes; `tail_has_recallable_inbound`). A lifecycle marker carries
  no inbound at all and is skipped too. Historical continuation notes likewise
  provide no fresh inbound.

Same-window dedup: the injected paths accumulate in `state.memory` via the
`memory` channel's reducer (`memory_state_merge`); this hook passes the
accumulated set into the recall call and writes back only the fresh paths, so a
note surfaces at most once per context window. A compaction summarizes the
injected notes away, so the record belongs to one window (`MemoryState.window`
is the `compact.version` it was written under) and the hook replaces it with an
empty one when the version moves on.

compact clobber-safety: auto-compact is also a before_llm hook. `messages`
carries the add_messages reducer, so co-writing it does NOT fail-loud — the
runner merges both hooks' values. But auto-compact's full-history REMOVE_ALL
replacement is order-sensitive and would swallow a note appended in the same
pass. So when auto-compact would fire this same turn, this hook defers
(returns None) rather than racing the history replacement; recall simply
retries on the next turn.
"""

from __future__ import annotations

__description__ = "Shared memory pool: meiva.memory SDK surface (PATH + PERSONAL_PATH + search) + passive recall hook (auto-surfaces relevant notes) + daily Meiva Dream consolidation skill (Dream-only validate.py, auto-OKF, index rebuild, commit, push, re-index)"

# meiva:note
# This module is the plugin's SDK **surface** — the only face an agent-launched
# child loads (task #3633). Its agent-runtime registrations (the two context-note
# indexes, the memory-discipline prompt section, the passive-recall before_llm
# hook) live in `agent_runtime.py`, imported only on the full path (see
# `agent/extensions/__init__.py`). The two `files.*` wraps below run in the exec
# subprocess and keep to the surface-only imports.


from base.packages.plugins.extensions import PluginContributions, SdkNamespace

from . import sdk as _memory_sdk


def contribute() -> PluginContributions:
    """What this plugin declares for the SDK surface."""
    return PluginContributions(
        sdk_namespaces=(SdkNamespace("memory", _memory_sdk, expand=False),),
    )
