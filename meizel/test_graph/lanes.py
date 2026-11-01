from __future__ import annotations

from collections import deque
from collections.abc import Mapping

from meizel.test_graph.model import (
    FACT_DYNAMIC_FIXTURE,
    FACT_DYNAMIC_IMPORT,
    FACT_IMPORT_ONLY,
    FACT_PYTESTER_NATIVE,
    FACT_SYNTAX_ERROR,
    FileTestFacts,
    ModuleTable,
    TestCheckout,
    TestLanes,
)

__all__ = [
    "NATIVE_FIXTURES",
    "NATIVE_MARKERS",
    "NATIVE_MODULES",
    "derive_test_lanes",
]

NATIVE_FIXTURES = frozenset(
    {
        "provisioned_db",
        "provisioned_redis",
        "_db_stall_guard",
        "_clean_db",
        "_clean_redis",
        "gateway_data_plane",
        "cluster_defaults_unset",
        "db_url",
        "database_version",
        "database_gate",
        "database",
        "redis_url",
        "event_bus",
        "db_conn",
        "adb_conn",
        "aops_pool",
        "aredis_inbound_listener",
        "postgres_access",
        "redis_access",
    }
)

NATIVE_MARKERS = frozenset({"requires_host", "flaky", "native_permissions_helper"})

NATIVE_MODULES = frozenset(
    {
        "base/deploy/tests/containers.py",
        "base/deploy/tests/pgbouncer.py",
        "scripts/ci/migration_smoke.py",
        "scripts/ci/pgvector_runtime_smoke.py",
        "scripts/ci/vendor_pg_runtime.py",
    }
)


def _ancestor_conftests(test_path: str, conftests: frozenset[str]) -> list[str]:
    found: list[str] = []
    directory = test_path.rpartition("/")[0]
    while True:
        candidate = f"{directory}/conftest.py" if directory else "conftest.py"
        if candidate in conftests:
            found.append(candidate)
        if not directory:
            break
        directory = directory.rpartition("/")[0]
    return found


def _plugin_closure(
    holder: str,
    facts_by_file: Mapping[str, FileTestFacts],
    table: ModuleTable,
) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    facts = facts_by_file.get(holder)
    queue: deque[str] = deque(facts.plugins if facts is not None else ())
    while queue:
        dotted = queue.popleft()
        mod_file = table.file(dotted)
        if mod_file is not None and mod_file not in seen:
            seen.add(mod_file)
            found.append(mod_file)
            sub_facts = facts_by_file.get(mod_file)
            if sub_facts is not None:
                queue.extend(sub_facts.plugins)
    return found


def _build_holder_fixtures(
    facts_by_file: Mapping[str, FileTestFacts],
) -> dict[str, dict[str, tuple[str, bool, tuple[str, ...]]]]:
    direct_fx: dict[str, dict[str, tuple[bool, tuple[str, ...]]]] = {
        rel: {name: (auto, reqs) for name, auto, reqs in f.fixtures}
        for rel, f in facts_by_file.items()
    }
    reexp_map: dict[str, dict[str, str]] = {
        rel: dict(f.reexports) for rel, f in facts_by_file.items()
    }

    def resolve_one(holder: str, fx_name: str) -> tuple[str, bool, tuple[str, ...]] | None:
        seen: set[tuple[str, str]] = set()
        curr_h, curr_fx = holder, fx_name
        while (curr_h, curr_fx) not in seen:
            seen.add((curr_h, curr_fx))
            own = direct_fx.get(curr_h, {}).get(curr_fx)
            if own is not None:
                return (curr_h, own[0], own[1])
            target = reexp_map.get(curr_h, {}).get(curr_fx)
            if target is None:
                return None
            curr_h, curr_fx = target.split(":", 1)
        return None

    table_by_holder: dict[str, dict[str, tuple[str, bool, tuple[str, ...]]]] = {}
    for rel in facts_by_file:
        entries: dict[str, tuple[str, bool, tuple[str, ...]]] = {}
        for name, (auto, reqs) in direct_fx.get(rel, {}).items():
            entries[name] = (rel, auto, reqs)
        for local_name in reexp_map.get(rel, {}):
            if local_name not in entries:
                resolved = resolve_one(rel, local_name)
                if resolved is not None:
                    entries[local_name] = resolved
        table_by_holder[rel] = entries
    return table_by_holder


def _path_scope_holders_by_test(
    checkout: TestCheckout,
    table: ModuleTable,
) -> dict[str, list[str]]:
    by_test: dict[str, list[str]] = {test: [] for test in checkout.collectable}
    for _source, dotted_mod, scopes in checkout.path_scopes:
        mod_file = table.file(dotted_mod)
        if mod_file is None:
            continue
        for scope in scopes:
            prefix = f"{scope}/"
            for test in checkout.collectable:
                if (test == scope or test.startswith(prefix)) and mod_file not in by_test[test]:
                    by_test[test].append(mod_file)
    return by_test


def _collect_fixture_holders(
    test_path: str,
    anc_conftests: list[str],
    ps_holders: list[str],
    facts_by_file: Mapping[str, FileTestFacts],
    table: ModuleTable,
) -> list[str]:
    holders: list[str] = [test_path, *_plugin_closure(test_path, facts_by_file, table)]
    for holder in (*ps_holders, *anc_conftests):
        if holder not in holders:
            holders.append(holder)
        for plugin_file in _plugin_closure(holder, facts_by_file, table):
            if plugin_file not in holders:
                holders.append(plugin_file)
    return holders


def _check_module_closure(
    seed_files: set[str],
    checkout: TestCheckout,
    facts_by_file: Mapping[str, FileTestFacts],
) -> str | None:
    mod_queue: deque[str] = deque(sorted(seed_files - checkout.global_files))
    visited: set[str] = set()
    while mod_queue:
        mod_path = mod_queue.popleft()
        if mod_path in visited:
            continue
        visited.add(mod_path)
        if mod_path in NATIVE_MODULES or mod_path in checkout.native_spawners:
            return f"native_module:{mod_path}"
        m_facts = facts_by_file.get(mod_path)
        if m_facts is None or (m_facts.flags & FACT_SYNTAX_ERROR):
            return f"syntax_error:{mod_path}"
        if m_facts.flags & FACT_DYNAMIC_IMPORT:
            return f"dynamic_import:{mod_path}"
        if (m_facts.flags & FACT_IMPORT_ONLY) and not mod_path.endswith("/__init__.py"):
            continue
        for nxt in m_facts.imports:
            if nxt not in visited and nxt not in checkout.global_files:
                mod_queue.append(nxt)
    return None


def _visible_fixtures_for_holders(
    holders: list[str],
    holder_fixtures: Mapping[str, Mapping[str, tuple[str, bool, tuple[str, ...]]]],
) -> tuple[dict[str, tuple[str, bool, tuple[str, ...]]], set[str]]:
    visible_fx: dict[str, tuple[str, bool, tuple[str, ...]]] = {}
    autouse_names: set[str] = set()
    for holder in holders:
        for name, entry in holder_fixtures.get(holder, {}).items():
            if name not in visible_fx:
                visible_fx[name] = entry
            if entry[1]:
                autouse_names.add(name)
    return visible_fx, autouse_names


def _walk_requested_fixtures(
    t_facts: FileTestFacts,
    visible_fx: Mapping[str, tuple[str, bool, tuple[str, ...]]],
    autouse_names: set[str],
    checkout: TestCheckout,
    facts_by_file: Mapping[str, FileTestFacts],
) -> tuple[str | None, set[str]]:
    req_queue: deque[str] = deque(sorted(set(t_facts.test_requests) | autouse_names))
    seen_fx: set[str] = set()
    active_fx_files: set[str] = set()
    while req_queue:
        fx = req_queue.popleft()
        if fx in seen_fx:
            continue
        seen_fx.add(fx)
        if fx in NATIVE_FIXTURES:
            return f"fixture:{fx}", active_fx_files
        if fx == "pytester" and (t_facts.flags & FACT_PYTESTER_NATIVE):
            return "fixture:pytester", active_fx_files
        entry = visible_fx.get(fx)
        if entry is None:
            continue
        def_file, _auto, sub_reqs = entry
        if def_file not in checkout.global_files:
            def_facts = facts_by_file.get(def_file)
            if def_facts is not None and (def_facts.flags & FACT_DYNAMIC_FIXTURE):
                return f"dynamic_fixture:{def_file}", active_fx_files
            active_fx_files.add(def_file)
        req_queue.extend(sub_reqs)
    return None, active_fx_files


def _module_closure_seeds(
    test_path: str,
    anc_conftests: list[str],
    ps_holders: list[str],
    active_fx_files: set[str],
    table: ModuleTable,
    tops: frozenset[str],
) -> set[str]:
    seed_files: set[str] = {test_path}
    for root_file in (
        test_path,
        *(cf for cf in anc_conftests if cf != "conftest.py"),
        *ps_holders,
        *active_fx_files,
    ):
        seed_files.add(root_file)
        dotted = ".".join(root_file.removesuffix(".py").split("/"))
        seed_files.update(table.module_files(dotted, tops))
    return seed_files


def _classify_one_test(
    test_path: str,
    checkout: TestCheckout,
    facts_by_file: Mapping[str, FileTestFacts],
    table: ModuleTable,
    holder_fixtures: Mapping[str, Mapping[str, tuple[str, bool, tuple[str, ...]]]],
    ps_holders: list[str],
) -> str | None:
    t_facts = facts_by_file.get(test_path)
    if t_facts is None or (t_facts.flags & FACT_SYNTAX_ERROR):
        return "syntax_error"
    native_marks = sorted(set(t_facts.markers) & NATIVE_MARKERS)
    if native_marks:
        return f"marker:{native_marks[0]}"
    if t_facts.flags & FACT_DYNAMIC_FIXTURE:
        return "dynamic_fixture"

    anc_conftests = _ancestor_conftests(test_path, checkout.conftests)
    holders = _collect_fixture_holders(test_path, anc_conftests, ps_holders, facts_by_file, table)
    visible_fx, autouse_names = _visible_fixtures_for_holders(holders, holder_fixtures)
    fx_reason, active_fx_files = _walk_requested_fixtures(
        t_facts, visible_fx, autouse_names, checkout, facts_by_file
    )
    if fx_reason is not None:
        return fx_reason
    seeds = _module_closure_seeds(
        test_path, anc_conftests, ps_holders, active_fx_files, table, checkout.top_packages
    )
    return _check_module_closure(seeds, checkout, facts_by_file)


# meiva:note
# Partitions all collectable tests in `checkout` into `static` and `native` lanes; any unknown,
# dynamic, or native-resource dependency routes the test to `native` with a diagnostic reason.
def derive_test_lanes(
    checkout: TestCheckout,
    facts_by_file: Mapping[str, FileTestFacts],
) -> TestLanes:
    table = ModuleTable.build(checkout.python_files, checkout.top_packages)
    holder_fixtures = _build_holder_fixtures(facts_by_file)
    ps_by_test = _path_scope_holders_by_test(checkout, table)

    static_files: set[str] = set()
    native_files: set[str] = set()
    reasons: list[tuple[str, str]] = []
    for test_path in sorted(checkout.collectable):
        reason = _classify_one_test(
            test_path,
            checkout,
            facts_by_file,
            table,
            holder_fixtures,
            ps_by_test.get(test_path, []),
        )
        if reason is None:
            static_files.add(test_path)
        else:
            native_files.add(test_path)
            reasons.append((test_path, reason))
    return TestLanes(
        static=frozenset(static_files),
        native=frozenset(native_files),
        reasons=tuple(reasons),
    )
