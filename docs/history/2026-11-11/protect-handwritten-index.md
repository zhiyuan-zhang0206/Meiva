# Preserve optional directory introductions

Directory index generation remains reachable through the standalone generator,
Steward and consolidation scripts. Preserve every existing index.md because
there is no reliable ownership marker distinguishing generated listings from
handwritten introductions. Exclusive creation closes the concurrent-create
overwrite window. Missing indexes still receive starter listings so existing
root pointers have targets; MEMORY.md Pointers continue to refresh.

Existing generated listings now remain unchanged too. Authors can remove a
listing when they explicitly want a fresh generated starter.
