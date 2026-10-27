import ast
from pathlib import Path

import pytest

from scripts.structure import placement
from scripts.structure.imports import facts
from scripts.structure.tests.patch_repo import make_repo

_SOURCE = """from pathlib import Path

def discover():
    paths: list[Path] = []
    local = Path(__file__).resolve().parents[2] / "base" / "plugins"
    for root in (local,):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if entry.name.startswith("."):
                continue
            member = entry / "config.json"
            if member.is_file():
                paths.append(member)
    return paths
"""


def _collect(root: Path, source: str = _SOURCE) -> facts.Evidence:
    return facts.collect(
        ast.parse(source),
        "cli/tests/test_probe.py",
        placement.ModuleIndex(root),
        tops=("base", "cli"),
    )


def test_domain_includes_future_and_removed_members_without_reading_runtime(tmp_path: Path) -> None:
    found = _collect(make_repo(tmp_path))
    members = [fact for fact in found.records if fact.member is not None]
    assert len(members) == 1
    domain = members[0]
    assert (domain.target, domain.member) == ("base/plugins", "config.json")
    assert found.unknown == ()
    assert domain.matches_member("base/plugins/future/config.json")
    assert domain.matches_member("base/plugins/removed/config.json")
    assert domain.matches_member("base/plugins/entry")
    assert domain.matches_member("base/plugins")
    assert not domain.matches_member("base/plugins/entry/tests/config.json")
    assert not domain.matches_member("base/plugins/entry/test_probe.py")
    assert not domain.matches_member("base/plugins2/entry/config.json")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("for root in (local,):", "sink(local)\n    for root in (local,):"),
        ("for root in (local,):", "local = other\n    for root in (local,):"),
        ("def discover():", "def discover():\n    global local"),
        ("def discover():", "@decorate\ndef discover():"),
        ("def discover():", "from opaque import *\n\ndef discover():"),
        ("def discover():", "type.__setattr__(Path, 'iterdir', other)\n\ndef discover():"),
        ("if not root.is_dir():", "sink(root)\n        if not root.is_dir():"),
        ("if not root.is_dir():", "root = other\n        if not root.is_dir():"),
        ("member = entry /", "sink(entry)\n            member = entry /"),
        ("member = entry /", "entry = other\n            member = entry /"),
        ("if member.is_file():", "sink(member)\n            if member.is_file():"),
        ("return paths", "sink(paths)\n    return paths"),
        ("return paths", "paths.append(other)\n    return paths"),
        ("return paths", "def capture():\n        return paths\n    return paths"),
        ("return paths", "return tuple(paths)"),
        ("return paths", "return []"),
        ("return paths", "paths = []\n    return paths"),
        ('"config.json"', "filename"),
        ('"config.json"', '"nested/config.json"'),
        ("sorted(root.iterdir())", 'root.glob("*")'),
        ("sorted(root.iterdir())", "sorted(other.iterdir())"),
        ("for root in (local,):", "sorted = other\n    for root in (local,):"),
        ("    return paths", "    else:\n        pass\n    return paths"),
        ("def discover():", "Path.iterdir = other\n\ndef discover():"),
        ("def discover():", 'setattr(Path, "is_file", other)\n\ndef discover():'),
        ("def discover():", 'patcher.setattr("pathlib.Path.iterdir", other)\n\ndef discover():'),
        (
            "def discover():",
            'from unittest.mock import patch\npatch("pathlib.Path.iterdir")\n\ndef discover():',
        ),
        ("return paths", 'exec("root = other")\n    return paths'),
        ("return paths", "sink(locals())\n    return paths"),
        ("return paths", 'eval("paths")\n    return paths'),
        ("return paths", "capture = lambda: paths\n    return paths"),
        ("return paths", "def capture():\n        return root\n    return paths"),
        ("return paths", "def capture():\n        return entry\n    return paths"),
        (
            "def discover():",
            'setter = patcher.setattr\nagain = setter\nagain("pathlib.Path.iterdir", other)\n\ndef discover():',
        ),
    ],
)
def test_unproven_or_escaping_discovery_keeps_coarse_edge(
    tmp_path: Path, old: str, new: str
) -> None:
    found = _collect(make_repo(tmp_path), _SOURCE.replace(old, new))
    assert not any(fact.member is not None for fact in found.records)
    assert any(fact.target == "base/plugins" and fact.member is None for fact in found.records)


def test_opaque_alternative_keeps_its_own_unknown(tmp_path: Path) -> None:
    found = _collect(make_repo(tmp_path), _SOURCE.replace("(local,)", "(local, external_root())"))
    assert any(fact.member == "config.json" for fact in found.records)
    assert any(
        gap.expression == "external_root()" and gap.reason == facts.UNANCHORED_READ
        for gap in found.unknown
    )


@pytest.mark.parametrize("alias", ["root", "entry", "member"])
def test_filesystem_alias_disables_the_proof(tmp_path: Path, alias: str) -> None:
    root = make_repo(tmp_path)
    plugins = root / "base/plugins"
    outside = root / "base/other"
    outside.mkdir()
    if alias == "root":
        plugins.symlink_to(outside, target_is_directory=True)
    else:
        plugins.mkdir()
        if alias == "entry":
            (plugins / "plugin").symlink_to(outside, target_is_directory=True)
        else:
            (plugins / "plugin").mkdir()
            (plugins / "plugin/config.json").symlink_to(outside / "data.json")
    found = _collect(root)
    assert not any(fact.member is not None for fact in found.records)
    assert any("filesystem alias" in gap.reason for gap in found.unknown)


def test_actual_mcp_reader_has_bounded_repo_and_opaque_external_domains() -> None:
    root = Path(__file__).resolve().parents[3]
    path = "base/packages/mcp_config.py"
    found = facts.collect(
        ast.parse((root / path).read_text()),
        path,
        placement.ModuleIndex(root),
        tops=("base", "meiva_builtins"),
    )
    assert [(fact.target, fact.member) for fact in found.records if fact.member is not None] == [
        ("meiva_builtins/plugins", ".mcp.json")
    ]
    assert not any(
        fact.target == "meiva_builtins/plugins" and fact.member is None for fact in found.records
    )
    assert any(
        gap.expression == "external_plugin_read_root(home=home)"
        and gap.reason == facts.UNANCHORED_READ
        for gap in found.unknown
    )


def test_dashboard_default_helper_discovery_stays_coarse() -> None:
    root = Path(__file__).resolve().parents[3]
    path = "base/packages/dashboard_supply.py"
    found = facts.collect(
        ast.parse((root / path).read_text()),
        path,
        placement.ModuleIndex(root),
        tops=("base", "meiva_builtins"),
    )
    assert any(
        fact.target == "meiva_builtins/plugins" and fact.member is None for fact in found.records
    )
    assert not any(fact.member is not None for fact in found.records)


@pytest.mark.parametrize("binding", ["global", "default"])
def test_global_or_default_root_is_not_a_local_discovery_proof(
    tmp_path: Path, binding: str
) -> None:
    assignment = '    local = Path(__file__).resolve().parents[2] / "base" / "plugins"\n'
    source = _SOURCE.replace(assignment, "")
    if binding == "global":
        source = source.replace("def discover():", assignment.strip() + "\n\ndef discover():")
    else:
        source = source.replace("def discover():", "def discover(" + assignment.strip() + "):")
    found = _collect(make_repo(tmp_path), source)
    assert not any(fact.member is not None for fact in found.records)
    assert any(fact.target == "base/plugins" for fact in found.records)


def test_visible_replacement_inside_a_called_helper_keeps_coarse_edge(tmp_path: Path) -> None:
    prefix = "def install_iterator():\n    Path.iterdir = lambda self: [self / 'one' / 'nested']\ninstall_iterator()\n\n"
    source = _SOURCE.replace("def discover():", prefix + "def discover():")
    found = _collect(make_repo(tmp_path), source)
    assert not any(fact.member is not None for fact in found.records)
    assert any(fact.target == "base/plugins" for fact in found.records)


@pytest.mark.parametrize(
    "replacement",
    [
        "import pytest\npatcher = pytest.MonkeyPatch()\nsetter = getattr(patcher, 'setattr')\nsetter('pathlib.Path.iterdir', lambda self: [self / 'one' / 'nested'])\n",
        "import builtins\nbuiltins.__dict__['sorted'] = lambda entries: [next(iter(entries)) / 'nested']\n",
        "from unittest.mock import patch as replace\ndef install():\n    replace('pathlib.Path.iterdir').start()\ninstall()\n",
        "from builtins import vars as reflect\n",
    ],
)
def test_visible_indirect_mutation_or_reflection_keeps_coarse_edge(
    tmp_path: Path, replacement: str
) -> None:
    source = _SOURCE.replace("def discover():", replacement + "\ndef discover():")
    found = _collect(make_repo(tmp_path), source)
    assert not any(fact.member is not None for fact in found.records)
    assert any(fact.target == "base/plugins" for fact in found.records)


def test_reflective_output_escape_cannot_hide_from_name_use_count(tmp_path: Path) -> None:
    source = _SOURCE.replace(
        "return paths",
        "vars()['paths'].append(vars()['local'] / 'one' / 'nested' / 'config.json')\n    return paths",
    )
    found = _collect(make_repo(tmp_path), source)
    assert not any(fact.member is not None for fact in found.records)
    assert any(fact.target == "base/plugins" for fact in found.records)


def test_global_builder_cannot_be_laundered_through_a_local_binding(tmp_path: Path) -> None:
    source = _SOURCE.replace(
        "def discover():",
        "ROOT = Path(__file__).resolve().parents[2]\nsink(ROOT)\n\ndef discover():",
    ).replace('Path(__file__).resolve().parents[2] / "base"', 'ROOT / "base"')
    found = _collect(make_repo(tmp_path), source)
    assert not any(fact.member is not None for fact in found.records)
    assert any(fact.target == "base/plugins" for fact in found.records)


@pytest.mark.parametrize("escape", ["global-builder", "output", "namespace"])
def test_failed_membership_proof_keeps_opaque_alternative(tmp_path: Path, escape: str) -> None:
    source = _SOURCE.replace("(local,)", "(local, external_root())")
    if escape == "global-builder":
        source = source.replace(
            "def discover():",
            "ROOT = Path(__file__).resolve().parents[2]\nsink(ROOT)\n\ndef discover():",
        ).replace('Path(__file__).resolve().parents[2] / "base"', 'ROOT / "base"')
    elif escape == "output":
        source = source.replace("return paths", "sink(paths)\n    return paths")
    else:
        source = source.replace("return paths", "sink(locals())\n    return paths")
    found = _collect(make_repo(tmp_path), source)
    assert not any(fact.member is not None for fact in found.records)
    assert any(fact.target == "base/plugins" for fact in found.records)
    assert any(
        gap.expression == "external_root()" and gap.reason == facts.UNANCHORED_READ
        for gap in found.unknown
    )
