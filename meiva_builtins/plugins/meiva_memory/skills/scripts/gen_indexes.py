import sys

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
    generate_directory_indexes,
    pool_dir,
    untracked_notes,
)

pool = pool_dir(resolve_meiva_home())
written = generate_directory_indexes(pool)

# meiva:note
# Say what the index cannot see. The generator deliberately reads the git index, so a note that
# was written but never staged is absent from the run -- and the run still exits 0 with a count,
# which reads exactly like success (2026-11-07: 6111 wrote eleven notes, ten reached the index,
# nothing complained). Naming the skipped files turns that silence into a line the author sees
# while they still have the notes in front of them. This reports; it does not fail and it does not
# add the draft to the bundle -- an untracked draft must stay invisible to the validator too.
skipped = untracked_notes(pool)
if skipped:
    shown = ", ".join(skipped[:8]) + (f" (+{len(skipped) - 8} more)" if len(skipped) > 8 else "")
    print(
        f"{len(skipped)} untracked .md not indexed (git add them, then run this again): {shown}",
        file=sys.stderr,
    )
