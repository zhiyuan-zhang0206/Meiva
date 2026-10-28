from __future__ import annotations

from pathlib import Path

import pytest

from meizel.allowances import parse
from meizel.graph.model import Code, MeizelError
from meizel.graph.universe import GitRepo, Universe
from meizel.language.typescript import scan_typescript


def test_ast_visits_expressions_but_not_text_and_respects_shadowing(tmp_path: Path) -> None:
    universe = Universe(GitRepo(tmp_path), ["src/view.tsx", "src/value.ts"])
    source = """
import { value } from './value'
const example = /import('missing')/;
const text = `import('missing') ${Date.now()}`;
const view = <div title="import('missing')">{value}</div>;
export const handler = (fetch: (x: string) => number): number => fetch('local');
export class Owner {
  method(Date: { now: () => number }): number { return Date.now() }
  callback = (): number => Math.random();
}
"""
    result = scan_typescript("src/view.tsx", source, universe)
    assert {fact.dst for fact in result.facts} == {"src/value.ts"}
    assert not result.problems
    calls = {item.key for item in result.purity if item.code is Code.IMPURE_CALL}
    assert "<module>#Date.now" in calls
    assert "Owner.callback#Math.random" in calls
    assert not any(key.endswith("#fetch") or key == "Owner.method#Date.now" for key in calls)


def test_ast_methods_arrows_assertions_and_any_use_shared_rule_inputs(tmp_path: Path) -> None:
    policy, _ = parse('[purity]\nmax-params = ["2"]\n')
    source = """
export class Owner {
  collaborator: any;
  run(a: number, b: number, c: number): any { return a as any }
}
export const arrow = (a: number, b: number, c: number): number => a + b + c;
const literal = { name: 'fixed' } as const;
const checked = { name: 'fixed' } satisfies { name: string };
"""
    result = scan_typescript("owner.ts", source, Universe(GitRepo(tmp_path), ["owner.ts"]), policy)
    assert {(item.code, item.key) for item in result.purity} >= {
        (Code.WIDE_PARAMS, "Owner.run"),
        (Code.WIDE_PARAMS, "arrow"),
    }
    assert [(site.qualname, site.slot) for site in result.any_collaborators] == [
        ("Owner", "collaborator"),
        ("Owner.run", "return"),
    ]
    assert [site.qualname for site in result.type_casts] == ["Owner.run"]
    assert {name for name, _, _ in result.symbols} == {"Owner", "arrow"}


def test_ast_uses_project_paths_without_policy_aliases(tmp_path: Path) -> None:
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions":{"paths":{"@app/*":["./src/*"]}}}')
    (tmp_path / "src").mkdir()
    (tmp_path / "src/value.ts").write_text("export const value = 1")
    universe = Universe(GitRepo(tmp_path), ["tsconfig.json", "src/owner.ts", "src/value.ts"])
    result = scan_typescript("src/owner.ts", "import {value} from '@app/value'", universe)
    assert [fact.dst for fact in result.facts] == ["src/value.ts"]
    assert not result.problems


def test_ast_reports_unresolved_internal_imports_and_computed_loaders(tmp_path: Path) -> None:
    result = scan_typescript(
        "owner.ts",
        "import './missing'; const next = import(target)",
        Universe(GitRepo(tmp_path), ["owner.ts"]),
    )
    assert len(result.problems) == 1
    assert [item.form for item in result.restricted] == ["import()"]
    assert all(problem.code is Code.UNRESOLVED_IMPORT for problem in result.problems)


def test_ast_parse_failure_is_not_an_empty_success(tmp_path: Path) -> None:
    with pytest.raises(MeizelError, match=r"cannot|expected|Expression"):
        scan_typescript(
            "broken.ts", "export const broken = ;", Universe(GitRepo(tmp_path), ["broken.ts"])
        )


def test_ast_patch_aliases_and_shadowed_vitest_names(tmp_path: Path) -> None:
    source = """
import {vi as tools} from 'vitest';
import * as collaborator from './service';
tools.spyOn(collaborator, 'read');
function helper(vi: {stubEnv: (a: string, b: string) => void}) { vi.stubEnv('KEY', 'value') }
"""
    result = scan_typescript(
        "owner.test.ts", source, Universe(GitRepo(tmp_path), ["owner.test.ts", "service.ts"])
    )
    assert [(patch.form, patch.target, patch.code) for patch in result.patches] == [
        ("vi.spyOn", "service.ts.read", Code.FIRST_PARTY_PATCH),
    ]


def test_namespace_access_and_require_use_lexical_bindings(tmp_path: Path) -> None:
    source = """
import * as service from './service.mjs';
service._hidden;
const other = require('./other.cjs');
other._internal;
function local(require: (name: string) => void) { require('./not-a-module') }
"""
    result = scan_typescript(
        "owner.cts", source, Universe(GitRepo(tmp_path), ["owner.cts", "service.mts", "other.cts"])
    )
    assert {(fact.dst, fact.names) for fact in result.facts if fact.names} == {
        ("service.mts", ("_hidden",)),
        ("other.cts", ("_internal",)),
    }
    assert not result.problems


def test_inferred_returns_and_value_constants_do_not_inflate_behavior(tmp_path: Path) -> None:
    source = "\n".join(f"export const value{i} = {i};" for i in range(20))
    source += "\nexport const key = (value: number) => ['key', value] as const;"
    result = scan_typescript("value.ts", source, Universe(GitRepo(tmp_path), ["value.ts"]))
    assert not {item.code for item in result.purity} & {
        Code.UNTYPED_EXPORT,
        Code.WIDE_PUBLIC_SURFACE,
        Code.UNFROZEN_VALUE,
    }


def test_module_resolution_keeps_each_importers_project_options(tmp_path: Path) -> None:
    from meizel.graph.model import Sources
    from meizel.language.typescript import scan_batch

    files = ["base.json"]
    (tmp_path / "base.json").write_text('{"compilerOptions":{"moduleResolution":"bundler"}}')
    contents: dict[str, str] = {}
    for folder in ("one", "two"):
        (tmp_path / folder).mkdir()
        config = f"{folder}/tsconfig.json"
        (tmp_path / config).write_text(
            '{"extends":"../base.json","compilerOptions":{"paths":{"@local/*":["./*"]}}}'
        )
        contents[f"{folder}/entry.ts"] = (
            "import {value} from '@local/value'; export const read = () => value;"
        )
        contents[f"{folder}/value.ts"] = "export const value = 1;"
        files.extend([config, f"{folder}/entry.ts", f"{folder}/value.ts"])
    policy, _ = parse("")
    scans = scan_batch(Universe(GitRepo(tmp_path), files), contents, policy, Sources())
    for folder in ("one", "two"):
        assert {fact.dst for fact in scans[f"{folder}/entry.ts"].facts} == {f"{folder}/value.ts"}


def test_enum_subsets_resolve_imported_enum_symbols(tmp_path: Path) -> None:
    from meizel.graph.model import Sources
    from meizel.language.typescript import scan_batch

    contents = {
        "kind.ts": "export enum Kind { One, Two, Three }",
        "use.ts": "import {Kind as Choice} from './kind'; export function pick(value: Choice): boolean { if (value === Choice.One || value === Choice.Two) return true; return false; }",
    }
    policy, _ = parse("")
    scans = scan_batch(Universe(GitRepo(tmp_path), list(contents)), contents, policy, Sources())
    assert [(site.cls_name, site.members) for site in scans["use.ts"].enum_subsets] == [
        ("kind.ts.Kind", ("One", "Two")),
    ]


def test_builtin_effects_follow_destructured_imports_without_confusing_local_names(
    tmp_path: Path,
) -> None:
    result = scan_typescript(
        "io.cts",
        """
const {readFileSync: read} = require('node:fs');
export const load = (): string => read('path', 'utf8');
export const pure = (read: () => string): string => read();
""",
        Universe(GitRepo(tmp_path), ["io.cts"]),
    )
    assert {item.key for item in result.purity if item.code is Code.IMPURE_CALL} == {
        "load#fs.readFileSync"
    }


def test_interface_and_function_type_boundaries_report_any(tmp_path: Path) -> None:
    source = """
export interface Port {
  collaborator: any;
  run(value: any): any;
}
export type Callback = (value: any) => any;
"""
    result = scan_typescript("port.ts", source, Universe(GitRepo(tmp_path), ["port.ts"]))
    assert {(site.qualname, site.slot) for site in result.any_collaborators} == {
        ("Port", "collaborator"),
        ("Port.run", "value"),
        ("Port.run", "return"),
        ("Callback.<call>", "value"),
        ("Callback.<call>", "return"),
    }
