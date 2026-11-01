from __future__ import annotations

import configparser
import hashlib
import tomllib
from collections import deque
from collections.abc import Iterable, Mapping
from typing import TypeGuard

from meizel.roles import parse_roles, pytest_test_hosts, select_packages
from meizel.test_graph.model import ModuleTable, RawFileTestFacts, TestCheckout

__all__ = ["build_test_checkout", "hash_test_tree", "is_test_config_file"]

_CODE_ROLE_NAMES = ("application", "standalone", "templates", "examples", "tooling")
_CONFIG_EXACT = frozenset(
    {"pyproject.toml", "pytest.ini", "meizel.policy.toml", "meizel.toml", "path_scopes.toml"}
)


def is_test_config_file(rel: str) -> bool:
    return rel in _CONFIG_EXACT or rel.endswith(("/meizel.toml", "/path_scopes.toml"))


def hash_test_tree(scanner_ver: str, digests: Mapping[str, str]) -> str:
    hasher = hashlib.sha256(f"{scanner_ver}\0".encode())
    for rel in sorted(digests):
        if (rel.endswith(".py") and not rel.startswith("tests/e2e/")) or is_test_config_file(rel):
            hasher.update(f"{rel}:{digests[rel]}\0".encode())
    return hasher.hexdigest()


def _is_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_array(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return _is_array(value) and all(isinstance(item, str) for item in value)


def _is_test_basename(name: str) -> bool:
    return name != "conftest.py" and (name.startswith("test_") or name.endswith("_test.py"))


def _discover_top_packages(
    python_files: tuple[str, ...],
    manifests: Mapping[str, str],
) -> frozenset[str]:
    root_manifest = manifests.get("meizel.toml")
    if root_manifest is not None and "[roles]" in root_manifest:
        roles = parse_roles(root_manifest, label="meizel.toml")
        selected = select_packages(roles, *_CODE_ROLE_NAMES)
        return frozenset((*selected, "tests"))
    tops = {rel.split("/", 1)[0] for rel in python_files if "/" in rel}
    return frozenset(tops | {"tests"})


def _parse_pytest_ini_patterns(ini_text: str) -> tuple[str, ...] | None:
    parser = configparser.ConfigParser()
    try:
        parser.read_string(ini_text)
    except configparser.Error:
        return None
    if not parser.has_option("pytest", "testpaths"):
        return None
    raw = parser.get("pytest", "testpaths")
    entries = tuple(token.strip("/") for token in raw.split() if token.strip("/"))
    return entries or None


def _matches_ini_pattern(rel: str, pattern: str) -> bool:
    parts = rel.split("/")
    if pattern == "tests":
        return parts[0] == "tests"
    if pattern.endswith("/**/tests"):
        prefix = pattern.removesuffix("/**/tests")
        return (rel.startswith(f"{prefix}/") or parts[0] == prefix) and "tests" in parts[:-1]
    return rel == pattern or rel.startswith(f"{pattern}/")


def _candidate_test_files(python_files: tuple[str, ...]) -> list[str]:
    return [
        rel
        for rel in python_files
        if not rel.startswith("tests/e2e/") and _is_test_basename(rel.rpartition("/")[2])
    ]


def _collectable_from_ini(candidates: list[str], ini_text: str) -> frozenset[str]:
    patterns = _parse_pytest_ini_patterns(ini_text)
    if patterns is None:
        return frozenset(candidates)
    return frozenset(
        rel for rel in candidates if any(_matches_ini_pattern(rel, pat) for pat in patterns)
    )


def _discover_collectable(
    python_files: tuple[str, ...],
    config_texts: Mapping[str, str],
) -> frozenset[str]:
    candidates = _candidate_test_files(python_files)
    pyproject = config_texts.get("pyproject.toml")
    if (
        pyproject is not None
        and "[tool.pytest.ini_options]" in pyproject
        and "testpaths" in pyproject
    ):
        hosts = frozenset(pytest_test_hosts(pyproject))
        return frozenset(
            rel
            for rel in candidates
            if rel.split("/", 1)[0] in hosts and "tests" in rel.split("/")[:-1]
        )
    ini_text = config_texts.get("pytest.ini")
    if ini_text is not None:
        return _collectable_from_ini(candidates, ini_text)
    return frozenset(rel for rel in candidates if "tests" in rel.split("/")[:-1])


def _tests_directories(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[: idx + 1]) for idx, part in enumerate(parts[:-1]) if part == "tests"]


def _parse_path_scopes(
    config_texts: Mapping[str, str],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    found: list[tuple[str, str, tuple[str, ...]]] = []
    for rel_path in sorted(config_texts):
        if rel_path != "path_scopes.toml" and not rel_path.endswith("/path_scopes.toml"):
            continue
        directory = rel_path.rpartition("/")[0]
        doc = tomllib.loads(config_texts[rel_path])
        for mod_name, raw_names in sorted(doc.items()):
            if not _is_str_list(raw_names):
                raise ValueError(f"{rel_path}: {mod_name} must be a list of path strings")
            scopes = tuple(
                directory if name == "." else (f"{directory}/{name}" if directory else name)
                for name in raw_names
            )
            found.append((rel_path, mod_name, scopes))
    return tuple(found)


def _merge_doc_allowances(
    doc: Mapping[str, object],
    prefix: str,
    loads: dict[str, set[str]],
    external: set[str],
) -> None:
    doc_loads = doc.get("loads")
    if _is_object(doc_loads):
        for key, targets in doc_loads.items():
            if _is_str_list(targets):
                full_key = f"{prefix}/{key}" if prefix else key
                loads.setdefault(full_key, set()).update(targets)
    doc_ext = doc.get("external")
    if _is_object(doc_ext):
        for key, reason in doc_ext.items():
            if isinstance(reason, str):
                full_key = f"{prefix}/{key}" if prefix else key
                external.add(full_key)


def _parse_loads_and_external(
    config_texts: Mapping[str, str],
    manifests: Mapping[str, str],
) -> tuple[tuple[tuple[str, tuple[str, ...]], ...], frozenset[str]]:
    loads: dict[str, set[str]] = {}
    external: set[str] = set()
    policy_text = config_texts.get("meizel.policy.toml")
    if policy_text:
        _merge_doc_allowances(tomllib.loads(policy_text), "", loads, external)
    for m_path, m_text in manifests.items():
        directory = m_path.rpartition("/")[0]
        try:
            doc = tomllib.loads(m_text)
        except tomllib.TOMLDecodeError:
            continue
        _merge_doc_allowances(doc, directory, loads, external)
    sorted_loads = tuple((k, tuple(sorted(v))) for k, v in sorted(loads.items()))
    return sorted_loads, frozenset(external)


def _global_import_candidates(raw: RawFileTestFacts) -> list[str]:
    candidates: list[str] = list(raw.plugins)
    for direct in raw.direct_imports:
        if direct.split(".", 1)[0] == "tests":
            candidates.append(direct)
    for level, mod, pairs in raw.from_imports:
        if level == 0 and mod.split(".", 1)[0] == "tests":
            candidates.append(mod)
            candidates.extend(f"{mod}.{orig}" for orig, _bound in pairs if orig != "*")
    return candidates


def _discover_global_files(
    raw_by_file: Mapping[str, RawFileTestFacts],
    table: ModuleTable,
    top_packages: frozenset[str],
) -> frozenset[str]:
    if "conftest.py" not in raw_by_file:
        return frozenset()
    global_set: set[str] = {"conftest.py"}
    queue: deque[str] = deque(["conftest.py"])
    while queue:
        raw = raw_by_file.get(queue.popleft())
        if raw is None:
            continue
        for dotted in _global_import_candidates(raw):
            resolved = table.resolve_prefix(dotted, top_packages)
            if resolved is None:
                continue
            for mfile in table.module_files(resolved, top_packages):
                if mfile not in global_set:
                    global_set.add(mfile)
                    queue.append(mfile)
    return frozenset(global_set)


def _discover_native_spawners(
    config_texts: Mapping[str, str],
    raw_by_file: Mapping[str, RawFileTestFacts],
) -> frozenset[str]:
    policy_text = config_texts.get("meizel.policy.toml")
    if not policy_text:
        return frozenset()
    doc = tomllib.loads(policy_text)
    single_owner = doc.get("single-owner")
    if not _is_object(single_owner):
        return frozenset()
    spawners: set[str] = set()
    for section in single_owner.values():
        if not _is_object(section):
            continue
        for rel_path in section:
            raw = raw_by_file.get(rel_path)
            if raw is not None and (
                "subprocess" in raw.direct_imports
                or any(mod.startswith("subprocess") for _lvl, mod, _pairs in raw.from_imports)
            ):
                spawners.add(rel_path)
    return frozenset(spawners)


# meiva:note
# Constructs a pure `TestCheckout` value from discovered paths, config texts, and raw AST facts.
def build_test_checkout(
    all_files: Iterable[str],
    config_texts: Mapping[str, str],
    raw_by_file: Mapping[str, RawFileTestFacts],
) -> TestCheckout:
    all_set = frozenset(all_files)
    python_files = tuple(sorted(p for p in all_set if p.endswith(".py")))
    manifests = {
        k: v for k, v in config_texts.items() if k == "meizel.toml" or k.endswith("/meizel.toml")
    }
    top_packages = _discover_top_packages(python_files, manifests)
    collectable = _discover_collectable(python_files, config_texts)
    test_dirs = frozenset(d for path in collectable for d in _tests_directories(path))
    conftests = frozenset(
        p for p in python_files if p == "conftest.py" or p.endswith("/conftest.py")
    )
    table = ModuleTable.build(python_files, top_packages)
    global_files = _discover_global_files(raw_by_file, table, top_packages)
    path_scopes = _parse_path_scopes(config_texts)
    policy_loads, policy_external = _parse_loads_and_external(config_texts, manifests)
    native_spawners = _discover_native_spawners(config_texts, raw_by_file)
    return TestCheckout(
        python_files=python_files,
        all_files=all_set,
        top_packages=top_packages,
        test_dirs=test_dirs,
        collectable=collectable,
        conftests=conftests,
        global_files=global_files,
        path_scopes=path_scopes,
        policy_loads=policy_loads,
        policy_external=policy_external,
        manifests=manifests,
        native_spawners=native_spawners,
    )
