from base.paths.home import resolve_meiva_home
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
    pool_dir,
    rebuild_root_memory_index,
)

# meiva:note
# The module rewrites MEMORY.md in place: everything up to the exact `## Pointers` heading line is preserved (a prose mention does not match), followed by one `- [title](rel) — description` line per root note and one `- [dir/](dir/index.md) — n notes` line per topic directory with subdirectory counts; the index cap is enforced by the pool's pre-commit hook (MEIVA_MEMORY_INDEX_CAP, default 16000), not by this script.
# meiva:note
# The counts this writes are a dated snapshot, not a claim about HEAD: the contract is that they
# agree with the tree of the commit that wrote them and are no older than three rebuild cycles
# (the daily health check enforces both). A root count that trails HEAD by up to one cycle is
# therefore correct, not a bug -- the per-directory indexes are regenerated inside every
# note-adding pull request, while this root file is rewritten once a day because recomputing it
# per pull request would rewrite an 11.5K-character file on every note and conflict whenever two
# pull requests are in flight.
# meiva:link meiva_builtins/plugins/meiva_memory/skills/scripts/docs/rebuild-memory-index.meiva.okf.md
rebuild_root_memory_index(pool_dir(resolve_meiva_home()))
