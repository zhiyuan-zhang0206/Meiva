from meiva_builtins.plugins.meiva_memory.pool_ops import (
    RESERVED as RESERVED,
)
from meiva_builtins.plugins.meiva_memory.pool_ops import (
    collect as collect,
)
from meiva_builtins.plugins.meiva_memory.pool_ops import (
    frontmatter as frontmatter,
)
from meiva_builtins.plugins.meiva_memory.pool_ops import (
    rebuild_root_memory_index,
)

# meiva:note
# The module rewrites MEMORY.md in place: everything up to the exact `## Pointers` heading line is preserved (a prose mention does not match), followed by one `- [title](rel) — description` line per root note and one `- [dir/](dir/index.md) — n notes` line per topic directory with subdirectory counts; the index cap is enforced by the pool's pre-commit hook (MEIVA_MEMORY_INDEX_CAP, default 16000), not by this script.
# meiva:link meiva_builtins/plugins/meiva_memory/skills/scripts/docs/rebuild-memory-index.meiva.okf.md
rebuild_root_memory_index()
