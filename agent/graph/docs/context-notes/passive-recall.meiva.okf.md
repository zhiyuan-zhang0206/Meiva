---
type: doc
title: Passive Recall — Score-Gated Memory Notes on Wake-Up Turns
description: How passive memory recall decides when to search, which hits it injects (decision-model score threshold, cap, same-window dedup), why a failed search is a logged no-op, and where its switch and defaults are stated.
tags:
- memory
- context
---

# Passive Recall — Score-Gated Memory Notes on Wake-Up Turns

## Responsibility

Passive recall (`memory_recall.py`, decisions in `memory_note.py`) injects memory
notes into the context behind the standing head without the agent asking. The
standing head itself — the SystemMessage and the ordered context notes — is
[[agent/graph/docs/context-notes/context-notes.meiva.okf.md|Standing Context]]'s
to state.

## Behavior

- Content-driven recall on **new inbound wake-up turns** (not agent-initiated search). The trigger gate skips machine-originated wake-ups (`watcher:` / `shell:` prefixes) and fires on user chat, peer-agent (`agent:`), scheduled (`schedule:`), and system inbound. **One search, scored by a decision model**: the gateway retrieves the best chunk of the closest `memory_rerank_candidates` (30) notes and a decision model (`memory_rerank_model`) scores each chunk's relevance to the conversation; recall injects the notes scoring at least `memory_recall_inject_threshold` (0.72), best first, at most `memory_recall_inject_max` (3), each with the passage that matched. Vector search always returns its top-k, so the score threshold is what keeps weak matches out of context; a turn where nothing reaches it injects nothing. Recall makes no model call of its own.
- Never fatal: a failed search (index or ranking model unavailable, or the gateway answering with an error status) → no-op, logged, and the next turn retries; the hook runs in `before_llm`, so an exception escaping it would end the agent process, not just the recall. There is no fallback to unscored vector order. Same-window dedup happens after the threshold and before the cap, so already-injected notes never use up an injection slot.
- The agent's `passive_memory_recall_enabled` slice field gates the stage. Which hits are injected (`agent/graph/recall/memory_note.py:qualifying`: score threshold, cap, present in the local pool) and how the note reads (`render_note_text`) are `strict` functions over the search hits; `passive_memory_recall` takes the local pool root as `memory_root` and reads which hit paths exist there. Its default, the threshold and the cluster opt-out are stated in [[meiva_builtins/plugins/meiva_memory/docs/memory-recall.meiva.okf.md]], not here.

## Entry Points

- `agent/graph/recall/memory_recall.py` — passive recall
- `agent/graph/recall/memory_note.py` — the pure injection decisions and note text

Parent: [[agent/graph/docs/context-notes/context-notes.meiva.okf.md|Standing Context]].
