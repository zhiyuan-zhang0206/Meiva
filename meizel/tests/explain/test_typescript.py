# meiva:note
# The shared receipt owns only temporary source files and completed subprocesses, not environment state.
# meiva:link meizel/docs/checker/typescript-evidence.meiva.okf.md

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import TypeGuard

import pytest

from meizel.contracts.cli import main


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_table(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(k, str) for k in value)


def _is_str_dict(value: object) -> TypeGuard[dict[str, str]]:
    return _is_table(value) and all(isinstance(v, str) for v in value.values())


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _as_table(value: object) -> dict[str, object]:
    assert _is_table(value)
    return value


def _as_str_dict(value: object) -> dict[str, str]:
    assert _is_str_dict(value)
    return value


def _as_object_list(value: object) -> list[object]:
    assert _is_object_list(value)
    return value


def _as_table_list(value: object) -> list[dict[str, object]]:
    items = _as_object_list(value)
    return [_as_table(item) for item in items]


def _as_str(value: object) -> str:
    assert isinstance(value, str)
    return value


@pytest.fixture(scope="module")
def receipt(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, set[str]]:
    # meiva:note
    # Fixed git and producer commands own only this temporary source receipt.
    # meiva:link meizel/docs/checker/typescript-evidence.meiva.okf.md
    root = tmp_path_factory.mktemp("ts-source-index")
    material = _as_str_dict(json.loads(Path(__file__).with_name("material.json").read_text()))
    files: set[str] = set()
    for name, source in material.items():
        relative = f"ui/web/src/lib/{name}.ts"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
        files.add(relative)
    (root / "meizel.policy.toml").write_text('[typescript]\nscopes=["ui/web/src/lib"]\n')
    (root / "ui/web/src/lib/meizel.toml").write_text('purity="strict"\n')
    (root / "plain.py").write_text("def value():\n    return 1\n")
    for args in (["git", "init", "-q", str(root)], ["git", "-C", str(root), "add", "-A"]):
        subprocess.run(args, check=True, capture_output=True)  # noqa: S603
    producer = Path(__file__).resolve().parents[2] / "source_evidence/typescript/producer.mjs"
    result = subprocess.run(  # noqa: S603
        ["node", str(producer), "--repo", str(root), *sorted(files)],
        check=True,
        capture_output=True,
        text=True,
    )
    index = root / "index.json"
    index.write_text(result.stdout)
    return root, index, files


def test_fixed_six_source_receipt(receipt: tuple[Path, Path, set[str]]) -> None:
    _, index, _ = receipt
    payload = _as_table(json.loads(index.read_text()))
    sources = _as_table_list(payload["sources"])
    by_name = {Path(_as_str(source["path"])).stem: source for source in sources}
    assert len(by_name) == 6
    for name in ("direct-map", "nested-map", "frozen-set"):
        [allocation] = _as_table_list(by_name[name]["observations"])
        assert allocation["kind"] == "module-allocation"
        assert allocation["declarations"]
    [nested] = _as_table_list(by_name["nested-map"]["observations"])
    assert nested["properties"] == ["cache"]
    [frozen] = _as_table_list(by_name["frozen-set"]["observations"])
    assert frozen["frozen_surface"] and frozen["freeze_receiver"] and frozen["freeze_callee"]
    for name in ("fresh-local", "rebound-map", "unknown-factory"):
        observations = _as_table_list(by_name[name]["observations"])
        assert all(item["kind"] == "unknown" for item in observations)
    rebound = _as_table_list(by_name["rebound-map"]["observations"])[-1]
    assert "source-shadowed" in _as_str(rebound["reason"])


def test_loader_refuses_invalid_identity_and_source_sets(
    receipt: tuple[Path, Path, set[str]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, index, _ = receipt
    original = _as_table(json.loads(index.read_text()))
    invalid: list[dict[str, object]] = []
    for field, value in (("version", 2), ("version", True), ("root", "/another-repository")):
        changed = copy.deepcopy(original)
        changed[field] = value
        invalid.append(changed)
    for field in ("sources", "dependencies"):
        changed = copy.deepcopy(original)
        entries = _as_table_list(changed[field])
        entries[0]["sha256"] = "0" * 64
        invalid.append(changed)
        missing = copy.deepcopy(original)
        _as_object_list(missing[field]).pop(0)
        invalid.append(missing)
    wrong_path = copy.deepcopy(original)
    _as_table_list(wrong_path["sources"])[0]["path"] = "../escape.ts"
    invalid.append(wrong_path)
    for payload in invalid:
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(payload))
        assert (
            main(
                [
                    "--repo",
                    str(root),
                    "--jobs",
                    "1",
                    "explain",
                    "ui/web/src/lib",
                    "--typescript-index",
                    str(bad),
                ]
            )
            == 2
        )
        assert capsys.readouterr().err


def test_cli_explicit_index_and_no_request_states(
    receipt: tuple[Path, Path, set[str]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    root, index, _ = receipt
    prefix = ["--repo", str(root), "--jobs", "1", "explain"]
    assert main([*prefix, "ui/web/src/lib", "--typescript-index", str(index)]) == 0
    shown = capsys.readouterr().out
    assert "independent AST source snapshot" in shown and "shallow frozen surface" in shown
    assert main([*prefix, "ui/web/src/lib"]) == 0
    assert "not-requested" in capsys.readouterr().out
    assert main([*prefix, "plain.py"]) == 0
    shown = capsys.readouterr().out
    assert "not-applicable" in shown and "Python source evidence" in shown
    bad = tmp_path / "bad.json"
    bad.write_text('{"version":1,"version":2}')
    assert main([*prefix, "ui/web/src/lib", "--typescript-index", str(bad)]) == 2
    assert "invalid TS source index" in capsys.readouterr().err


def test_parse_failure_is_unavailable_instead_of_empty_safe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # meiva:note
    # Fixed commands build a temporary parse-refusal receipt without executing source.
    # meiva:link meizel/docs/checker/typescript-evidence.meiva.okf.md
    root = tmp_path
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)  # noqa: S603
    (root / "fault.ts").write_text("const = ;\n")
    subprocess.run(["git", "-C", str(root), "add", "fault.ts"], check=True, capture_output=True)  # noqa: S603
    producer = Path(__file__).resolve().parents[2] / "source_evidence/typescript/producer.mjs"
    result = subprocess.run(  # noqa: S603
        ["node", str(producer), "--repo", str(root), "fault.ts"],
        check=True,
        capture_output=True,
        text=True,
    )
    index = root / "unavailable.json"
    index.write_text(result.stdout)
    assert (
        main(
            [
                "--repo",
                str(root),
                "--jobs",
                "1",
                "explain",
                "fault.ts",
                "--typescript-index",
                str(index),
            ]
        )
        == 0
    )
    shown = capsys.readouterr().out
    assert "TS source evidence: unavailable" in shown
    assert "unanalysed behavior remains unknown" in shown


def test_frozen_holder_keeps_collection_surface_and_literal_path_separate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # meiva:note
    # Fixed commands build the exact wrapper/key receipt in an isolated temporary repository.
    # meiva:link meizel/docs/checker/typescript-evidence.meiva.okf.md
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)  # noqa: S603
    source = tmp_path / "holder.ts"
    source.write_text(
        'export const state = Object.freeze({"a.b": new Map()});\n'
        "export const seen = Object.freeze(new Set());\n"
    )
    subprocess.run(  # noqa: S603
        ["git", "-C", str(tmp_path), "add", "holder.ts"], check=True, capture_output=True
    )
    producer = Path(__file__).resolve().parents[2] / "source_evidence/typescript/producer.mjs"
    result = subprocess.run(  # noqa: S603
        ["node", str(producer), "--repo", str(tmp_path), "holder.ts"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = _as_table(json.loads(result.stdout))
    [receipt] = _as_table_list(payload["sources"])
    holder, direct = _as_table_list(receipt["observations"])
    assert holder["properties"] == ["a.b"]
    assert holder["frozen_surface"] is False
    assert holder["freeze_receiver"] == holder["freeze_callee"] == []
    assert direct["frozen_surface"] is True
    index = tmp_path / "index.json"
    index.write_text(result.stdout)
    assert (
        main(
            [
                "--repo",
                str(tmp_path),
                "--jobs",
                "1",
                "explain",
                "holder.ts",
                "--typescript-index",
                str(index),
            ]
        )
        == 0
    )
    shown = capsys.readouterr().out
    assert 'state["a.b"]: module-allocation Map;' in shown
    assert "seen: module-allocation Set; shallow frozen surface only" in shown
