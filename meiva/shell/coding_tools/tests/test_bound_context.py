from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from base.agents.context import MeivaContext
from base.clock.ids import new_uuid
from base.clock.tests.fakes import clock_at
from base.clock.tests.seeded_rng import seeded_rng
from base.sessions import coding_session_owner
from base.sessions.backend import get_shell_backend
from meiva.sdk_surface import process_context
from meiva.shell.coding_tools import _common, claude, codex

INSTANT = dt.datetime(2031, 3, 4, 5, 6, tzinfo=dt.UTC)


# meiva:note
# A bound context whose home is not the environment's, whose clock stands at one instant and whose
# generator is seeded: a coding tool that read the environment, the real clock or a generator of its
# own instead would not find what these tests look for.
@pytest.fixture
def context_home(tmp_path: Path, unit_home: Path) -> Iterator[Path]:
    home = tmp_path / "context-home"
    home.mkdir()
    assert home != unit_home
    process_context.bind_context(
        MeivaContext(meiva_home=home, clock_factory=lambda: clock_at(INSTANT), rng=seeded_rng(11))
    )
    yield home


def _launched(
    workspace: Path, home: Path, tool: str
) -> tuple[coding_session_owner.CodingSessionKey, coding_session_owner.CodingSessionOwner]:
    workspace.mkdir(exist_ok=True)
    key = coding_session_owner.canonical_key(workspace, tool=tool, cluster=home)
    owner = _common.new_generation(
        key,
        owner_agent_id=41,
        tasks_file=None,
        work_file=None,
        ttl_seconds=3600,
        sessions=get_shell_backend(home=home),
    )
    return key, owner


def test_the_shell_sessions_of_a_cancel_are_the_bound_contexts_not_the_environments(
    context_home: Path,
) -> None:
    assert _common.cluster_sessions().session_log_path("meiva-x") == (
        context_home / "logs" / "meiva-x.out.log"
    )


def test_the_cluster_home_is_the_bound_contexts_not_the_environments(
    context_home: Path, unit_home: Path
) -> None:
    assert _common.cluster_home() == context_home
    assert _common.cluster_home() != unit_home


def test_a_new_generation_draws_from_the_bound_generator_and_stamps_the_bound_instant(
    context_home: Path, tmp_path: Path
) -> None:
    _key, owner = _launched(tmp_path / "workspace", context_home, "codex")

    assert owner.generation == str(new_uuid(seeded_rng(11)))
    assert owner.created_at == INSTANT


@pytest.mark.parametrize("tool_module", [claude, codex])
def test_status_and_cancel_work_on_the_records_under_the_bound_contexts_home(
    tool_module: ModuleType,
    context_home: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tool = tool_module.__name__.rsplit(".", 1)[-1]
    workspace = tmp_path / "workspace"
    key, owner = _launched(workspace, context_home, tool)
    assert owner.generation is not None

    assert tool_module.status(workspace) == 0
    assert f"generation={owner.generation}" in capsys.readouterr().out

    assert tool_module.cancel(workspace, owner.generation) == 0
    assert coding_session_owner.read(key, owner.generation).terminalized_at == INSTANT


# meiva:note
# The probe cache is written only where a probed CLI is installed, so this test pins the cache's place
# on a host that has one and holds trivially on a host that has none: it never lands in the
# environment's home.
@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_the_probe_cache_goes_to_the_bound_contexts_home(
    tool: str, context_home: Path, unit_home: Path
) -> None:
    _common.capability_report(tool)

    assert not (unit_home / "host" / "tool-probes.json").exists()
    cache = context_home / "host" / "tool-probes.json"
    assert not cache.exists() or cache.stat().st_size > 0
