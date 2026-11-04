# meiva:note
# meiva_memory — the `services.py` face `services.supervision.roster.plugin_services` loads by file path. The
# declaration itself lives in `service_declaration/declaration.py`: pyright resolves an import
# against the checked directory's root first, so a root-level `services.py` shadows the repository's
# `services` package for every file beside it, and the declaration imports from that package. This
# file only re-exports the `services()` hook under the name the loader looks for.
# meiva:link meiva_builtins/plugins/meiva_memory/docs/meiva_memory.meiva.okf.md
from meiva_builtins.plugins.meiva_memory.service_declaration.declaration import services as services
