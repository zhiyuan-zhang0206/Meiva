# Workers — prompt, budget and handoff

Reference for Phase 2. The root skill carries the rules; this file carries the
prompt to copy, the arithmetic, and the incident that made the budget rule.

## The worker prompt

Make it self-contained: a worker starts with no context and cannot ask.

```
You are a Deep Research worker for sub-question: <sub-question>.
Full question: <three-part question>. Audience: <Z>. Evidence bar: <bar>.
Run these queries: <queries>. Prefer primary sources; open pages before
trusting snippets. One reformulation per query if the first returns nothing.
Your fetch cap: <cap> meiva.web.fetch calls — COUNT them and stop at the cap;
sources beyond it go unread, not uncited.
Never fabricate a learning — if the sub-question is unanswerable, write
learnings: [] plus a "note" explaining what is missing.
When done: write {handoff}/learnings-{n}.json and {handoff}/sources-{n}.json
conforming to the schema in references/research-state.md (source ids are
integers; every learning's source_ids must exist in your sources file).
Do NOT message anyone — writing the files IS the handoff. Then terminate.
```

## The fetch cap is one global budget

Search and fetch are cheap (user ruling 2026-08-09), so `max_fetches` bounds
worker drift rather than API spend. With N workers, each counting only its own
calls, N budgets add up to ~N× the cap — the #1108 failure mode: 60 registered,
100 actual.

Distribute the cap so the parts sum exactly to the whole:

```
cap_i = max_fetches // n, plus 1 to the first (max_fetches % n) workers
```

Put each worker's own number in its prompt, and let the audit's global check
(unique sources ≤ `max_fetches`) prove the distribution held.
