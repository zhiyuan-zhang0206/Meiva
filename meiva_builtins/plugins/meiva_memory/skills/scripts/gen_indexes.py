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
)

written = generate_directory_indexes(pool_dir(resolve_meiva_home()))
