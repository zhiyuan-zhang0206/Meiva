# meiva:note
# The pure decisions of a sealed release: the manifest's form and strictness, what counts as a
# difference, and what staging and switching decide from the facts they are handed.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from base.deploy.sealed_release import build_plan, sealed_plan, stage_plan

_FILE = sealed_plan.Entry(sealed_plan.EntryKind.FILE, 0o444, 3, 11, 100, 200, sha256="a" * 64)
_DIR = sealed_plan.Entry(sealed_plan.EntryKind.DIR, 0o555, 96, 10, 100, 200)
_LINK = sealed_plan.Entry(sealed_plan.EntryKind.LINK, 0o755, 9, 12, 100, 200, target="../python")


def _tree() -> sealed_plan.Manifest:
    return sealed_plan.Manifest(
        root="/r", entries={".": _DIR, "a.py": _FILE, ".venv/bin/python": _LINK}
    )


def test_manifest_round_trips_through_json() -> None:
    manifest = _tree()
    assert sealed_plan.Manifest.parse(manifest.to_json(), origin="m") == manifest


_DROP = object()
_FILE_ROW = ["file", 0o444, 3, 11, 100, 200, None, "a" * 64]


@pytest.mark.parametrize(
    ("where", "value", "message"),
    [
        (("schema",), "meiva-sealed-tree/v0", "schema"),
        (("extra",), 1, "exactly"),
        (("root",), "relative", "absolute"),
        (("entries", "../escape"), _FILE_ROW, "relative"),
        (("entries", "."), _DROP, "root entry"),
        (("entries", "a.py", 0), "socket", "kind"),
        (("entries", "a.py", 2), -1, "non-negative"),
        (("entries", "a.py", 1), True, "non-negative"),
        (("entries", "a.py", 7), "A" * 64, "SHA-256"),
        (("entries", "a.py", 6), "x", "only a link"),
        (("entries", ".venv/bin/python", 6), None, "names its target"),
        (("entries", ".", 7), "a" * 64, "only a file"),
    ],
)
def test_a_malformed_manifest_fails_fast(
    where: tuple[str | int, ...], value: object, message: str
) -> None:
    body: Any = json.loads(_tree().to_json())
    *parents, last = where
    target: Any = body
    for key in parents:
        target = target[key]
    if value is _DROP:
        del target[last]
    else:
        target[last] = value
    with pytest.raises(sealed_plan.ManifestError, match=message):
        sealed_plan.Manifest.parse(json.dumps(body), origin="m")


def test_an_identical_tree_has_no_differences() -> None:
    sealed = _tree()
    assert sealed_plan.differences(sealed, "/r", sealed.entries, content=True) == ()


def test_differences_name_moved_missing_added_and_changed_paths() -> None:
    sealed = _tree()
    observed = {
        ".": _DIR,
        "a.py": sealed_plan.Entry(
            sealed_plan.EntryKind.FILE, 0o644, 3, 11, 100, 300, sha256="a" * 64
        ),
        "new.py": _FILE,
    }
    assert sealed_plan.differences(sealed, "/elsewhere", observed, content=False) == (
        "moved: sealed at /r, found at /elsewhere",
        "missing: .venv/bin/python",
        "added: new.py",
        "changed: a.py (mode, ctime_ns)",
    )


# meiva:note
# Without `content` an observed file carries no digest, and that absence is not a change; with
# it, a digest that differs is.
def test_the_content_digest_counts_only_when_content_is_compared() -> None:
    sealed = _tree()
    observed = {
        **sealed.entries,
        "a.py": sealed_plan.Entry(sealed_plan.EntryKind.FILE, 0o444, 3, 11, 100, 200),
    }
    assert sealed_plan.differences(sealed, "/r", observed, content=False) == ()
    rehashed = {
        **sealed.entries,
        "a.py": sealed_plan.Entry(
            sealed_plan.EntryKind.FILE, 0o444, 3, 11, 100, 200, sha256="b" * 64
        ),
    }
    assert sealed_plan.differences(sealed, "/r", rehashed, content=True) == (
        "changed: a.py (sha256)",
    )


def test_report_keeps_the_first_findings_and_counts_the_rest() -> None:
    findings = [f"added: {index}" for index in range(5)]
    assert sealed_plan.report(findings, limit=5) == tuple(findings)
    assert sealed_plan.report(findings, limit=2) == ("added: 0", "added: 1", "... and 3 more")


def test_the_manifest_sits_beside_its_release() -> None:
    release = Path("/h/source-releases/42-0123456789ab")
    assert sealed_plan.manifest_path(release) == Path(
        "/h/source-releases/42-0123456789ab.manifest.json"
    )


def test_release_names_lead_with_the_code_version() -> None:
    assert stage_plan.release_id("0123456789abcdef" * 2 + "01234567", 4821) == "4821-0123456789ab"
    assert stage_plan.releases_dir(Path("/h")) == Path("/h/source-releases")
    assert stage_plan.source_link(Path("/h")) == Path("/h/source")


def test_a_sealed_release_is_reused_and_an_unfinished_one_rebuilt() -> None:
    assert (
        stage_plan.stage_action(sealed=True, present=True, live=True)
        is stage_plan.StageAction.REUSE
    )
    assert (
        stage_plan.stage_action(sealed=False, present=True, live=False)
        is stage_plan.StageAction.REBUILD
    )
    assert (
        stage_plan.stage_action(sealed=False, present=False, live=False)
        is stage_plan.StageAction.BUILD
    )


def test_a_stage_never_rebuilds_the_release_source_runs_from() -> None:
    with pytest.raises(stage_plan.StagePlanError, match="unfinished release"):
        stage_plan.stage_action(sealed=False, present=True, live=True)


def test_a_switch_needs_an_unchanged_release_of_this_home_and_a_pointer() -> None:
    assert (
        stage_plan.switch_refusal(link=stage_plan.LinkKind.POINTER, own_release=True, findings=())
        is None
    )
    assert (
        stage_plan.switch_refusal(link=stage_plan.LinkKind.ABSENT, own_release=True, findings=())
        is None
    )
    assert "not a directory of this home" in str(
        stage_plan.switch_refusal(link=stage_plan.LinkKind.POINTER, own_release=False, findings=())
    )
    assert "differs from its manifest: added: x" in str(
        stage_plan.switch_refusal(
            link=stage_plan.LinkKind.POINTER, own_release=True, findings=("added: x",)
        )
    )
    assert "one-time cutover" in str(
        stage_plan.switch_refusal(link=stage_plan.LinkKind.OTHER, own_release=True, findings=())
    )


def test_the_link_mode_is_named_so_every_release_file_is_its_own_inode() -> None:
    assert build_plan.sync_argv("/bin/uv", system="linux")[-1] == "--link-mode=copy"
    assert build_plan.sync_argv("/bin/uv", system="darwin") == [
        "/bin/uv",
        "sync",
        "--frozen",
        "--compile-bytecode",
        "--link-mode=clone",
    ]


def test_the_build_drops_an_inherited_virtualenv() -> None:
    environ = {"PATH": "/bin", "VIRTUAL_ENV": "/prod/.venv", "UV_PROJECT_ENVIRONMENT": "/x"}
    assert build_plan.build_env(environ) == {"PATH": "/bin"}


def test_compileall_skips_the_venv_uv_already_compiled() -> None:
    argv = build_plan.compile_argv(Path("/r/.venv/bin/python"), Path("/r"))
    assert argv[:3] == ["/r/.venv/bin/python", "-m", "compileall"]
    assert argv[-1] == "/r"
    assert r"\.venv/" in argv[argv.index("-x") + 1]


def test_commit_times_are_recorded_in_utc() -> None:
    assert build_plan.utc_iso("2026-11-02T17:21:00+08:00") == "2026-11-02T09:21:00+00:00"
