from __future__ import annotations

# meiva:note
# The package door. `route_target` is the one routing rule two packages share — claim
# decides where a batch goes, and `init_context` re-applies the same rewrite when it
# jumps to a compaction's parked destination — so it is exported here rather than
# reached for through `_routing`.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
from ._routing import route_target

__all__ = ["route_target"]
