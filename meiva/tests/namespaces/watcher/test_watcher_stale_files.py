# meiva:note
# Which generated watcher files a launch removes, decided on values (`launch_plan.stale_names`). The disk steps around it and the order the launch reads the directory and the live sessions in are covered end to end in test_watcher_files.py.

from __future__ import annotations

import pathlib

import pytest

from meiva.watcher import launch_plan

_KINDS = ("watcher_{}.py", "watcher_{}_boot.py", "watcher_{}_launch.sh")


def _pair(session_id: int) -> list[str]:
    return [kind.format(session_id) for kind in _KINDS]


@pytest.mark.parametrize("kind", _KINDS)
def test_a_generated_file_of_a_dead_session_is_stale(kind: str) -> None:
    assert launch_plan.stale_names([kind.format(7)], alive=frozenset()) == (kind.format(7),)


@pytest.mark.parametrize("kind", _KINDS)
def test_a_generated_file_of_a_live_session_is_kept(kind: str) -> None:
    assert launch_plan.stale_names([kind.format(7)], alive=frozenset({7})) == ()


def test_only_the_dead_sessions_pairs_are_stale() -> None:
    names = [*_pair(3), *_pair(4), *_pair(5)]

    assert launch_plan.stale_names(names, alive={4}) == (*_pair(3), *_pair(5))


def test_a_session_id_is_matched_whole_not_by_prefix() -> None:
    assert launch_plan.stale_names(_pair(12), alive={1}) == tuple(_pair(12))
    assert launch_plan.stale_names(_pair(1), alive={12}) == tuple(_pair(1))


@pytest.mark.parametrize(
    "name",
    [
        "keep_me.py",
        "watcher_.py",
        "watcher_x.py",
        "watcher_7_other.py",
        "watcher_7_boot.pyc",
        "my_watcher_7.py",
        "watcher_7.py.bak",
        "notes.txt",
    ],
)
def test_a_name_the_launch_did_not_generate_is_left_alone(name: str) -> None:
    assert launch_plan.stale_names([name], alive=frozenset()) == ()


def test_every_file_the_launch_plans_is_recognized_as_its_session_s_own() -> None:
    request = launch_plan.SpawnRequest(code="pass\n", watchdog_secs=None, name="w", ttl_seconds=60)
    planned = launch_plan.plan_files(pathlib.Path("/watchers"), 9, request, 1, "python")
    names = [
        planned.script_path.name,
        planned.boot_path.name,
        planned.launch_path.name,
    ]

    assert launch_plan.stale_names(names, alive=frozenset()) == tuple(names)
    assert launch_plan.stale_names(names, alive={9}) == ()
