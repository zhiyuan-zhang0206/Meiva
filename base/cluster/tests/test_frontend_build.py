# meiva:link base/cluster/docs/frontend-build.meiva.okf.md

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from base.cluster import frontend_build


def _recording_build(calls: list[Path]) -> Callable[[Path], int]:
    def build(frontend_dir: Path) -> int:
        calls.append(frontend_dir)
        return 0

    return build


def _fixed_key(value: str | None) -> Callable[..., str | None]:
    def key(_dir: Path, *, gateway_port: int, browser_origin: str) -> str | None:
        return value

    return key


def test_a_matching_key_skips_the_build(tmp_path: Path) -> None:
    calls: list[Path] = []
    frontend_build.record(tmp_path, "k")
    code = frontend_build.ensure_build(
        tmp_path,
        gateway_port=8000,
        browser_origin="",
        build=_recording_build(calls),
        key=_fixed_key("k"),
    )

    assert code == 0
    assert calls == []


def test_a_changed_key_rebuilds_and_records(tmp_path: Path) -> None:
    calls: list[Path] = []
    frontend_build.record(tmp_path, "old")
    code = frontend_build.ensure_build(
        tmp_path,
        gateway_port=8000,
        browser_origin="",
        build=_recording_build(calls),
        key=_fixed_key("new"),
    )

    assert (code, calls) == (0, [tmp_path])
    assert frontend_build.recorded_key(tmp_path) == "new"


def test_a_failed_build_records_nothing(tmp_path: Path) -> None:
    frontend_build.record(tmp_path, "old")

    def failing(_dir: Path) -> int:
        return 3

    code = frontend_build.ensure_build(
        tmp_path, gateway_port=8000, browser_origin="", build=failing, key=_fixed_key("new")
    )

    assert code == 3
    assert frontend_build.recorded_key(tmp_path) == "old"


def test_an_unprovable_key_always_builds(tmp_path: Path) -> None:
    calls: list[Path] = []
    code = frontend_build.ensure_build(
        tmp_path,
        gateway_port=8000,
        browser_origin="",
        build=_recording_build(calls),
        key=_fixed_key(None),
    )

    assert (code, calls) == (0, [tmp_path])
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
