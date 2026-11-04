# meiva:note
# meiva_memory — ops service declarations (the plugin's `build_services()` hook).
#
# The memory indexer is the pool's search side: it watches the gateway's consolidated checkout and
# keeps the memory search index current, which is what makes `meiva.memory.search` — and therefore
# passive recall — return anything. It is declared here rather than hardcoded into
# `services/supervision/roster/composition.py` because the pool is this plugin's. Its host-scoped
# `indexer_enabled` config controls the daemon separately from whether agents inject the standing
# memory index into their prompts.
#
# Discovery keys on this plugin's code being PRESENT on the machine (see
# `services.supervision.roster.plugin_services.plugin_services`), so the cluster-level on/off is the
# explicit gate below rather than the presence check.
#
# Deliberately light, like `meiva_fleet/task_maintenance/declaration.py`: it imports the ops service
# contract and roster probe helper plus `base` — never `plugin.py` or the memory domain code — so
# the ops/CLI/watchdog process that discovers it does not pull in the agent kernel. `services()` is
# a function so probe ports derived from settings are read at use-time.
#
# It lives in this subdirectory, behind the `services.py` re-export the loader reads, because pyright
# resolves an import against the checked directory's root first: a root-level `services.py` would
# shadow the repository's `services` package for every file beside it.
# meiva:link meiva_builtins/plugins/meiva_memory/docs/meiva_memory.meiva.okf.md
from __future__ import annotations

from functools import partial
from pathlib import Path

from base.cluster.machine import MachineRole
from base.daemon.endpoints import ServiceEndpoints
from base.packages.plugins.config_registration import disk_image_path, read_config_image
from meiva_builtins.plugins.meiva_memory.default_config import MemoryConfig
from services.supervision.roster import healthz_daemon
from services.supervision.roster.service_spec import ServiceSpec

# meiva:note
# The indexer runs on the gateway capability: it indexes the gateway's consolidated checkout, which
# only a gateway-capable unit has. Declared here rather than reaching into ops's private `_GATEWAY`
# so the plugin owns its own capability decision.
_GATEWAY: frozenset[MachineRole] = frozenset({"gateway"})


# meiva:note
# Gate the indexer from the validated host config, independently of agent injection.
def _memory_indexer_gate(config: MemoryConfig) -> str | None:
    if not config.indexer_enabled:
        return "disabled (meiva_memory.indexer_enabled off)"
    return None


# meiva:note
# The ops services the memory plugin contributes to the roster: the gateway-side indexing daemon.
# Ordering against memory-search (which it cold-start-connects to) is preserved by
# `plugin_services()` folding plugin services onto the tail of the roster, well after the gateway
# group. The pool is a markdown checkout on disk and the default index backends (numpy) never open
# the main DB (which is also why it is the one daemon that skips `assert_schema_current`), so a pg
# outage or a schema mismatch is not its concern and the watchdog keeps reviving it
# (`requires_db=False`). The selectable pgvector backend does dial it, so the launcher still
# delivers the gateway login (`db_access="gateway"`).
def services(*, home: Path, endpoints: ServiceEndpoints) -> tuple[ServiceSpec, ...]:
    config_path = disk_image_path("meiva_memory", home=home)
    config = read_config_image(MemoryConfig, config_path)
    return (
        healthz_daemon(
            "memory-indexer",
            home=home,
            endpoints=endpoints,
            module="services.derived.memory_indexer.daemon",
            capabilities=_GATEWAY,
            requires_db=False,
            db_access="gateway",
            gate=partial(_memory_indexer_gate, config),
            config_inputs=(config_path,),
        ),
    )
