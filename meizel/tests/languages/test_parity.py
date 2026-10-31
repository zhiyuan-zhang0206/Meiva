from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, analysis_of, commit, diagnostics, git, run, write


@pytest.mark.parametrize("extension", ["py", "ts", "tsx", "mts", "cts"])
def test_both_languages_share_check_record_prune_and_strict_modes(
    make_repo: RepoFactory, extension: str
) -> None:
    path = f"pkg/value.{extension}"
    source = (
        "def value(a: int, b: int, c: int, d: int, e: int, f: int, g: int) -> int:\n    return a + b\n"
        if extension == "py"
        else "export const value = (a: number, b: number, c: number, d: number, e: number, f: number, g: number): number => a + b;"
    )
    root = make_repo({path: source, "pkg/meizel.toml": 'purity = "strict-to-be"\n'})
    [diagnostic] = diagnostics(root, "wide-params")
    assert diagnostic.path == path and diagnostic.debt is not None
    assert run(root, "check") == 1
    assert run(root, "debt", "record", "pkg") == 0
    git(root, "add", "-A")
    assert run(root, "check") == 0
    fixed = (
        "def value() -> int:\n    return 1\n"
        if extension == "py"
        else "export const value = (): number => 1;"
    )
    write(root, {path: fixed})
    assert run(root, "debt", "prune") == 0
    assert "wide-params" not in tomllib.loads((root / "pkg/meizel.toml").read_text()).get(
        "debt", {}
    )
    write(root, {path: source, "pkg/meizel.toml": 'purity = "strict"\n'})
    strict = diagnostics(root, "wide-params")
    assert len(strict) == 1 and strict[0].debt is None
    assert run(root, "debt", "record", "pkg") == 1


@pytest.mark.parametrize("extension", ["py", "ts"])
def test_languages_share_architectural_rule_diagnostics(
    make_repo: RepoFactory, extension: str
) -> None:
    source = (
        "from typing import Any, cast\nimport time\n\ndef inspect(value: Any) -> Any:\n"
        "    value.owner.state.name = 'changed'\n    time.time()\n    return cast(object, value.owner.state.deep.name)\n"
        if extension == "py"
        else "export function inspect(value: any): any { value.owner.state.name = 'changed'; Date.now(); return value.owner.state.deep.name as object; }"
    )
    root = make_repo(
        {
            f"pkg/inspect.{extension}": source,
            "pkg/meizel.toml": 'purity = "strict-to-be"\n',
            "meizel.toml": '[roles]\napplication = ["pkg"]\n',
        }
    )
    codes = {d.code.value for d in analysis_of(root).diagnostics}
    assert codes >= {
        "any-collaborator",
        "type-cast",
        "nested-attr-write",
        "deep-attr-chain",
        "ambient-read",
        "impure-call",
    }


def test_typescript_debt_symbol_move_uses_ast_spans(make_repo: RepoFactory) -> None:
    moved = "export const moved = (): number => { const text = '}'; return Date.now() + text.length; };\n"
    kept = "export const kept = (): number => Date.now();\n"
    root = make_repo({"pkg/old.ts": moved + kept, "pkg/meizel.toml": 'purity = "strict-to-be"\n'})
    assert run(root, "debt", "record", "pkg") == 0
    git(root, "add", "-A")
    commit(root)
    write(root, {"pkg/old.ts": kept, "pkg/new.ts": moved})
    assert run(root, "debt", "carry", "--move-symbol", "pkg/old.ts::moved=pkg/new.ts") == 0
    git(root, "add", "-A")
    assert run(root, "check") == 0
    debt = tomllib.loads((root / "pkg/meizel.toml").read_text())["debt"]
    assert debt["impure-call"] == {"new.ts#moved#Date.now": 1, "old.ts#kept#Date.now": 1}


# meiva:note
# The fixed Python probe receives an empty PATH to exercise missing Node without changing
# process-wide environment or replacing the production subprocess collaborator.
def test_missing_node_is_an_error_only_when_typescript_is_selected(tmp_path: Path) -> None:
    for path, expected in (("value.py", 0), ("value.ts", 2)):
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "meizel.tests.languages.compiler_probe", path, str(tmp_path)],
            env={"PATH": ""},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == expected, result.stderr
        if expected:
            assert "TypeScript compiler unavailable" in result.stdout


@pytest.mark.parametrize("extension", ["py", "ts"])
def test_dynamic_loads_use_the_same_manifest_contract(
    make_repo: RepoFactory, extension: str
) -> None:
    source = (
        "import importlib\ndef load(name):\n    return importlib.import_module(name)\n"
        if extension == "py"
        else "export const load = (name: string) => import(name);"
    )
    root = make_repo(
        {
            f"app/load.{extension}": source,
            "app/meizel.toml": f'[loads]\n"load.{extension}" = ["plugins"]\n',
            f"plugins/value.{extension}": "value = 1"
            if extension == "py"
            else "export const value = 1;",
            "meizel.toml": '[uses]\napp = ["plugins"]\n',
        }
    )
    assert run(root, "check") == 0
    write(root, {f"app/load.{extension}": ""})
    assert "stale-load" in {d.code.value for d in analysis_of(root).verdict}


@pytest.mark.parametrize("extension", ["py", "ts"])
def test_ambient_roots_and_state_contracts_cover_both_languages(
    make_repo: RepoFactory, extension: str
) -> None:
    source = (
        "import time\nsettings = {'flag': True}\ndef now():\n    return time.time()\n"
        if extension == "py"
        else "export const settings = {flag: true}; export const now = () => Date.now();"
    )
    reader = (
        "from app.runtime import settings\ndef read():\n    return settings['flag']\n"
        if extension == "py"
        else "import {settings as config} from './runtime'; export const read = () => config.flag;"
    )
    root = make_repo(
        {
            f"app/runtime.{extension}": source,
            f"app/reader.{extension}": reader,
            "app/meizel.toml": f'[uses]\n"reader.{extension}" = ["runtime.{extension}"]\n[state]\n"runtime.{extension}" = ["settings"]\n[roots]\n"runtime.{extension}" = {{ kinds = ["clock"], why = "reads the clock" }}\n',
            "meizel.toml": '[roles]\nfrontend = ["app"]\n',
            "meizel.policy.toml": '[ambient]\nsettings = ["settings"]\n',
        }
    )
    diagnostics = analysis_of(root).diagnostics
    assert not any(d.code.value == "manifest-unknown-child" for d in diagnostics)
    assert not any(
        d.code.value == "ambient-read" and d.path == f"app/runtime.{extension}" for d in diagnostics
    )
    assert any(
        d.code.value == "ambient-read" and d.path == f"app/reader.{extension}" for d in diagnostics
    )


@pytest.mark.parametrize("extension", ["py", "ts"])
def test_external_loader_declaration_is_language_neutral(
    make_repo: RepoFactory, extension: str
) -> None:
    source = (
        "import importlib\ndef load(name):\n    return importlib.import_module(name)\n"
        if extension == "py"
        else "export const load = (name: string) => import(name);"
    )
    root = make_repo(
        {
            f"loader.{extension}": source,
            "meizel.toml": f'[external]\n"loader.{extension}" = "loads operator-selected extensions"\n',
        }
    )
    assert run(root, "check") == 0
    write(root, {f"loader.{extension}": ""})
    assert "stale-external" in {d.code.value for d in analysis_of(root).verdict}


def test_typescript_rule_comparison_refreshes_policy_after_the_old_snapshot(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(
        {
            "app/view.ts": "export const value = afterCall();\n",
            "app/meizel.toml": 'purity = "strict-to-be"\n',
            "rules/engine.py": "RULE = 1\n",
            "meizel.policy.toml": '[typescript]\nimpure-calls = ["beforeCall"]\n',
        }
    )
    commit(root)
    write(
        root,
        {
            "rules/engine.py": "RULE = 2\n",
            "meizel.policy.toml": '[typescript]\nimpure-calls = ["afterCall"]\n',
        },
    )
    assert run(root, "debt", "record", ".") == 0
    write(root, {})
    capsys.readouterr()
    assert (
        run(
            root,
            "debt",
            "diff",
            "--against",
            "HEAD",
            "--on-growth",
            "fail",
            "--waive-if-changed",
            ":(glob)rules/**",
        )
        == 0
    )
    row = next(
        line.split()
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("impure-call")
    )
    assert row[1:] == ["0", "1", "1", "+1", "+0"]


@pytest.mark.parametrize("extension", ["py", "ts"])
def test_malformed_policy_reports_the_same_diagnostic_for_both_languages(
    make_repo: RepoFactory, extension: str
) -> None:
    root = make_repo(
        {
            f"value.{extension}": "value = 1" if extension == "py" else "export const value = 1;",
            "meizel.policy.toml": "this is not toml",
        }
    )
    [error] = diagnostics(root, "policy-syntax")
    assert error.path == "meizel.policy.toml"
    assert run(root, "check") == 1


VITEST_SETUP = 'import { vi } from "vitest";\n\nvi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));\n'
VITEST_REPO = {"ui/tests/setup.ts": VITEST_SETUP, "meizel.toml": '[roles]\nfrontend = ["ui"]\n'}


def approving(path: str) -> str:
    return f'[approved-patches.files]\n"{path}" = "the global setup of the runner"\n'


@pytest.mark.parametrize(
    ("policy", "expected"),
    [("", {"third-party-patch"}), (approving("ui/tests/setup.ts"), set[str]())],
)
def test_a_typescript_setup_file_is_approved_like_a_python_one(
    make_repo: RepoFactory, policy: str, expected: set[str]
) -> None:
    root = make_repo({**VITEST_REPO, "meizel.policy.toml": policy})
    found = analysis_of(root).diagnostics
    assert {d.code.value for d in found if d.path == "ui/tests/setup.ts"} == expected


@pytest.mark.parametrize("name", ["view.test.ts", "view.spec.ts", "view.test.tsx", "view.spec.tsx"])
def test_a_typescript_test_module_cannot_be_approved(make_repo: RepoFactory, name: str) -> None:
    path = f"ui/tests/{name}"
    root = make_repo({**VITEST_REPO, path: VITEST_SETUP, "meizel.policy.toml": approving(path)})
    [refused] = [d for d in analysis_of(root).diagnostics if d.code.value == "policy-syntax"]
    assert "a test module" in refused.message


ROOT = Path(__file__).resolve().parents[3]
# meiva:note
# An approved file keeps its patches out of every baseline, so nothing else stops one more from being
# added: the number of patch sites each holds is pinned here, and a change to it (or to the list) is a
# deliberate edit that a reviewer reads. `python -m meizel debt approved` prints these numbers. The
# TypeScript file needs the compiler, which is why this test is in a file of the native lane.
APPROVED_PATCH_SITES = {
    "cli/tests/bootstrap/child_main_dispatch.py": 2,
    "tests/e2e/fakes/child_budget_writer.py": 2,
    "tests/fixtures/guards.py": 7,
    "tests/fixtures/provisioning.py": 2,
    "tests/integration/child_schedule_runner.py": 2,
    "ui/web/tests/vitest.setup.ts": 1,
}


def test_the_patch_sites_of_the_approved_files_are_pinned(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(ROOT, "debt", "approved", "--json") == 0
    found = {item["path"]: item["sites"] for item in json.loads(capsys.readouterr().out)}
    assert found == APPROVED_PATCH_SITES


SHARED_MOCK = {
    "ui/state-test-mock.tsx": "export const state = {count: 0};\n",
    "meizel.toml": '[roles]\nfrontend = ["ui"]\n',
}
MOCK_WRITE = 'import {state} from "./state-test-mock";\nstate.count = 1;\n'
MOCK_PRODUCTION = (
    'import {state} from "./state-test-mock";\nexport const count = (): number => state.count;\n'
)


def test_a_typescript_patch_of_a_shared_mock_is_not_a_patch_of_the_code_under_test(
    make_repo: RepoFactory,
) -> None:
    root = make_repo({**SHARED_MOCK, "ui/view.test.tsx": MOCK_WRITE})
    assert diagnostics(root, "first-party-patch") == []


def test_a_typescript_mock_of_a_shared_mock_is_not_recorded(make_repo: RepoFactory) -> None:
    mock = 'import {vi} from "vitest";\nvi.mock("./state-test-mock");\n'
    root = make_repo({**SHARED_MOCK, "ui/view.test.tsx": mock})
    assert diagnostics(root, "first-party-patch") == []


def test_a_production_import_of_the_shared_mock_brings_every_patch_back(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {**SHARED_MOCK, "ui/view.test.tsx": MOCK_WRITE, "ui/view.tsx": MOCK_PRODUCTION}
    )
    [found] = diagnostics(root, "first-party-patch")
    assert found.path == "ui/view.test.tsx"
    assert "state-test-mock.tsx.count" in found.message
