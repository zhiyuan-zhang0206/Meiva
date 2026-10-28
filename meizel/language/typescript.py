from __future__ import annotations

from meizel import allowances
from meizel.allowances import Policy
from meizel.graph.model import Sources
from meizel.graph.universe import Universe
from meizel.language.typescript_compiler import TsConfig, TsScan, load_ts_config, scan_batch

__all__ = [
    "TsConfig",
    "TsScan",
    "load_ts_config",
    "scan_batch",
    "scan_typescript",
    "typescript_files",
    "typescript_scopes",
]


def typescript_files(universe: Universe, policy: Policy) -> tuple[str, ...]:
    config = load_ts_config(policy)
    active = (*config.scopes, *config.import_sources)
    return tuple(
        path
        for path in universe.typescript_files
        if path.endswith(config.extensions)
        and (not active or any(path == p or path.startswith(f"{p}/") for p in active))
    )


# meiva:note
# The directories whose TypeScript behavior this repository judges (`[typescript].scopes`): the
# caller needs the list, not the whole config.
def typescript_scopes(policy: Policy) -> tuple[str, ...]:
    return load_ts_config(policy).scopes


def scan_typescript(
    path: str, source: str, universe: Universe, policy: Policy | None = None
) -> TsScan:
    loaded = allowances.load(universe.root) if policy is None else policy
    return scan_batch(universe, {path: source}, loaded, Sources())[path]
