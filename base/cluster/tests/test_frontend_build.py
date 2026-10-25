# meiva:link base/cluster/docs/frontend-build.meiva.okf.md

from __future__ import annotations

from pathlib import Path

from base.cluster import frontend_build


def test_a_matching_key_skips_the_build(tmp_path: Path) -> None:
    frontend_build.record(tmp_path, "k")
    decision = frontend_build.plan_frontend_build(
        tmp_path,
        current_key="k",
        recorded=frontend_build.recorded_key(tmp_path),
    )

    assert decision.action == "reuse"
    assert "reusing .next" in decision.message


def test_a_changed_key_rebuilds_and_records(tmp_path: Path) -> None:
    frontend_build.record(tmp_path, "old")
    decision = frontend_build.plan_frontend_build(
        tmp_path,
        current_key="new",
        recorded=frontend_build.recorded_key(tmp_path),
    )
    assert decision.action == "build"
    code = frontend_build.finalize_frontend_build(tmp_path, current_key="new", exit_code=0)

    assert code == 0
    assert frontend_build.recorded_key(tmp_path) == "new"


def test_a_failed_build_records_nothing(tmp_path: Path) -> None:
    frontend_build.record(tmp_path, "old")
    decision = frontend_build.plan_frontend_build(
        tmp_path,
        current_key="new",
        recorded=frontend_build.recorded_key(tmp_path),
    )
    assert decision.action == "build"
    code = frontend_build.finalize_frontend_build(tmp_path, current_key="new", exit_code=3)

    assert code == 3
    assert frontend_build.recorded_key(tmp_path) == "old"


def test_an_unprovable_key_always_builds(tmp_path: Path) -> None:
    decision = frontend_build.plan_frontend_build(
        tmp_path,
        current_key=None,
        recorded=frontend_build.recorded_key(tmp_path),
    )
    assert decision.action == "build"
    code = frontend_build.finalize_frontend_build(tmp_path, current_key=None, exit_code=0)

    assert code == 0
    assert frontend_build.recorded_key(tmp_path) is None


def test_the_key_binds_the_baked_env_and_node() -> None:
    first = frontend_build.key_from_parts("source", "v22.0.0", gateway_port=8000, browser_origin="")
    same = frontend_build.key_from_parts("source", "v22.0.0", gateway_port=8000, browser_origin="")
    other_port = frontend_build.key_from_parts(
        "source", "v22.0.0", gateway_port=8001, browser_origin=""
    )
    other_source = frontend_build.key_from_parts(
        "source2", "v22.0.0", gateway_port=8000, browser_origin=""
    )

    assert first == same
    assert first != other_port
    assert first != other_source
