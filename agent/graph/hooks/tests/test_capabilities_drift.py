"""The `# Capabilities` index is a snapshot; the skill catalog under it is not.

`init_context` renders the index into the SystemMessage once per context window,
while `meiva.skills.names()` re-scans the filesystem on every call. These pin the
mechanism that keeps the two from drifting apart without waiting for a
compaction: the membership snapshot recorded at build time, the diff against it,
and the one note that names what appeared.

Skills are faked by running in a per-test unit home (`unit_home`), same shape as
agent/graph/tests/prompt/test_capabilities_index.py.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.hooks.capabilities import newly_installed_skills
from agent.graph.hooks.framework import framework_hooks
from agent.graph.prompt.capabilities import NEW_SKILLS_MAX_ENTRIES
from agent.graph.prompt.live import skill_view
from agent.graph.prompt.skill_selection import index_drift, indexed_skill_identifiers
from agent.state import AgentState, CapabilitiesState
from base import paths
from base.agents.context import MeivaContext
from base.agents.messages.kwargs import NoteTag
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.packages.extensions import install_registry
from base.packages.extensions.install_registry import InstalledPackage
from base.packages.plugin_providers import build_model_catalog

_CONFIG: RunnableConfig = {"configurable": {"thread_id": "1042"}}


def _slices(pins: Mapping[str, Any] | None = None) -> AgentSlices:
    return AgentSlices.resolve(
        {"skills_to_inject_into_system_prompt": ["*"], **(pins or {})},
        default_reader=defaults(),
    )


# meiva:link agent/graph/docs/system-prompt/capability-index-freshness.meiva.okf.md
def _runtime(
    *,
    ops_pool: AsyncConnectionPool | None,
    database_gate: ProcessDbGate,
    pins: Mapping[str, Any] | None = None,
) -> Runtime[MeivaContext]:
    return Runtime(
        context=MeivaContext(
            ops_pool=ops_pool,
            agent=_slices(pins),
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(),
            catalog=build_model_catalog(),
            clock_factory=Clock.from_settings,
        )
    )


@pytest.fixture
def skills_dir(unit_home: Path) -> Path:
    d = paths.skills_dir(paths.meiva_home())
    d.mkdir()
    return d


def _install(root: Path, name: str, desc: str) -> None:
    """Drop a skill onto disk the way an install does — mid-session, with no
    notification to any running agent."""
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nBODY\n", encoding="utf-8"
    )
    install_registry.register(InstalledPackage(name=name, type="skill"))


def _state(indexed: set[str] | None) -> AgentState:
    return AgentState(capabilities=CapabilitiesState(indexed=indexed))


async def _run_hook(
    indexed: set[str] | None,
    database_gate: ProcessDbGate,
    ops_pool: AsyncConnectionPool,
    *,
    pins: Mapping[str, Any] | None = None,
) -> dict | None:
    return await newly_installed_skills(
        _state(indexed),
        _runtime(ops_pool=ops_pool, database_gate=database_gate, pins=pins),
        _CONFIG,
    )  # type: ignore[arg-type]


# ── the diff ──


def test_drift_reports_only_what_appeared_since_the_snapshot(skills_dir: Path) -> None:
    """The whole point: a skill installed after the index was rendered shows up
    as an addition, and one that was already indexed does not show up again."""
    _install(skills_dir, "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers(skill_view(_slices().prompt))
    assert snapshot == {"alpha"}

    _install(skills_dir, "beta", "Beta desc")

    drift = index_drift(
        snapshot,
        skill_view(_slices().prompt),
    )
    assert [s["name"] for s in drift.added] == ["beta"]
    assert drift.identifiers == {"alpha", "beta"}


def test_drift_is_empty_when_the_catalog_has_not_moved(skills_dir: Path) -> None:
    _install(skills_dir, "alpha", "Alpha desc")
    drift = index_drift(
        indexed_skill_identifiers(skill_view(_slices().prompt)),
        skill_view(_slices().prompt),
    )
    assert drift.added == []


def test_uninstall_leaves_the_snapshot_so_a_reinstall_announces_again(
    skills_dir: Path,
) -> None:
    """Only additions are reported, but membership is replaced rather than
    unioned — otherwise a skill removed and put back would stay silent."""
    _install(skills_dir, "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers(skill_view(_slices().prompt))

    shutil.rmtree(skills_dir / "alpha")
    after_removal = index_drift(
        snapshot,
        skill_view(_slices().prompt),
    )
    assert after_removal.added == []
    assert after_removal.identifiers == set()

    _install(skills_dir, "alpha", "Alpha desc")
    assert [
        s["name"]
        for s in index_drift(
            after_removal.identifiers,
            skill_view(_slices().prompt),
        ).added
    ] == ["alpha"]


# ── the note ──


async def test_hook_names_the_new_skill_in_the_index_line_shape(
    skills_dir: Path, database_gate: ProcessDbGate, aops_pool: AsyncConnectionPool
) -> None:
    """The note has to read as more of the `# Capabilities` listing, so it uses
    the same `- \\`meiva.skills.<path>\\` — description` line the index does."""
    _install(skills_dir, "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers(skill_view(_slices().prompt))
    _install(skills_dir, "beta", "Beta desc")

    update = await _run_hook(snapshot, database_gate, aops_pool)

    assert update is not None
    (note,) = update["messages"]
    assert note.additional_kwargs["meiva_note_tag"] == NoteTag.NEW_SKILLS.value  # pyright: ignore[reportUnknownMemberType]
    assert "- `meiva.skills.beta` — Beta desc" in note.content  # pyright: ignore[reportUnknownMemberType]
    assert "alpha" not in note.content  # pyright: ignore[reportUnknownMemberType]


async def test_hook_advances_the_snapshot_so_one_install_is_named_once(
    skills_dir: Path, database_gate: ProcessDbGate, aops_pool: AsyncConnectionPool
) -> None:
    _install(skills_dir, "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers(skill_view(_slices().prompt))
    _install(skills_dir, "beta", "Beta desc")

    update = await _run_hook(snapshot, database_gate, aops_pool)
    assert update is not None
    advanced = update["capabilities"].indexed  # pyright: ignore[reportUnknownMemberType]
    assert advanced == {"alpha", "beta"}

    assert await _run_hook(advanced, database_gate, aops_pool) is None  # pyright: ignore[reportUnknownArgumentType]


async def test_hook_is_silent_when_nothing_was_installed(
    skills_dir: Path, database_gate: ProcessDbGate, aops_pool: AsyncConnectionPool
) -> None:
    _install(skills_dir, "alpha", "Alpha desc")
    assert (
        await _run_hook(
            indexed_skill_identifiers(skill_view(_slices().prompt)),
            database_gate,
            aops_pool,
        )
        is None
    )


async def test_no_snapshot_adopts_the_live_catalog_without_announcing_it(
    skills_dir: Path, database_gate: ProcessDbGate, aops_pool: AsyncConnectionPool
) -> None:
    """A checkpoint written before the snapshot field existed. What that agent's
    standing SystemMessage lists is unknowable here, so the whole catalog must
    not be announced as newly installed."""
    _install(skills_dir, "alpha", "Alpha desc")
    _install(skills_dir, "beta", "Beta desc")

    update = await _run_hook(None, database_gate, aops_pool)

    assert update is not None
    assert "messages" not in update
    assert update["capabilities"].indexed == {"alpha", "beta"}  # pyright: ignore[reportUnknownMemberType]


async def test_a_bulk_install_is_capped_with_a_counted_tail(
    skills_dir: Path, database_gate: ProcessDbGate, aops_pool: AsyncConnectionPool
) -> None:
    """One drift event can carry dozens — a first converge on a fresh box, a
    plugin sync landing a whole pack. The note must not turn into a second full
    index, so it lists a bounded prefix and points at the catalog for the rest."""
    extra = 5
    for i in range(NEW_SKILLS_MAX_ENTRIES + extra):
        _install(skills_dir, f"skill-{i:02d}", f"Desc {i}")

    update = await _run_hook(set(), database_gate, aops_pool)

    assert update is not None
    (note,) = update["messages"]
    lines = [ln for ln in note.content.splitlines() if ln.startswith("- ")]  # pyright: ignore[reportUnknownMemberType]
    assert (
        len(lines) == NEW_SKILLS_MAX_ENTRIES + 1  # pyright: ignore[reportUnknownArgumentType]
    )  # the cap, plus the tail  # pyright: ignore[reportUnknownArgumentType]
    assert lines[-1].startswith(f"- … and {extra} more")  # pyright: ignore[reportUnknownMemberType]
    assert "meiva.help(meiva.skills)" in lines[-1]
    # The snapshot still covers every one of them, so the ones the note left out
    # are never named later — the tail's pointer is what covers them.
    assert len(update["capabilities"].indexed) == NEW_SKILLS_MAX_ENTRIES + extra  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


# ── the two turns that write nothing ──


async def test_hook_defers_when_a_compaction_will_replace_the_window(
    skills_dir: Path, database_gate: ProcessDbGate, aops_pool: AsyncConnectionPool
) -> None:
    """Real drift, but a compaction fires the same before_llm pass — so nothing
    may be written at all.

    `add_messages` applies compaction's `REMOVE_ALL` and THEN the append, so a
    note written here is the sole survivor of the wipe, and `init_context` (whose
    only trigger is an empty `messages`) would read that one note as an intact
    history and drop the parked summary. agent/graph/tests/test_init_context.py drives
    that composition end to end; this pins the predicate the defer is gated on.
    """
    _install(skills_dir, "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers(skill_view(_slices().prompt))
    _install(skills_dir, "beta", "Beta desc")
    over_the_ceiling: list[AnyMessage] = [
        SystemMessage(content="<sys>"),
        *(HumanMessage(content="x" * 1000, id=f"h{i}") for i in range(5)),
    ]
    state = AgentState(messages=over_the_ceiling, capabilities=CapabilitiesState(indexed=snapshot))

    assert (
        await newly_installed_skills(
            state,
            _runtime(
                ops_pool=aops_pool,
                database_gate=database_gate,
                pins={"auto_compact_ceiling_tokens": 1},
            ),
            _CONFIG,
        )
        is None
    )  # type: ignore[arg-type]

    # Deferring is not silencing: the same state with no compaction predicted
    # still names beta, so the guard is the only thing suppressing it.
    update = await newly_installed_skills(
        state, _runtime(ops_pool=aops_pool, database_gate=database_gate), _CONFIG
    )  # type: ignore[arg-type]
    assert update is not None
    assert "meiva.skills.beta" in update["messages"][0].content  # pyright: ignore[reportUnknownMemberType]


async def test_container_mode_writes_nothing(
    skills_dir: Path, database_gate: ProcessDbGate, aops_pool: AsyncConnectionPool
) -> None:
    """The eval harness has no ops_pool — the same signal `init_context` reduces
    its head by. An eval's context is deterministic by construction, and this
    note's trigger is whatever the host filesystem gained mid-run."""
    _install(skills_dir, "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers(skill_view(_slices().prompt))
    _install(skills_dir, "beta", "Beta desc")

    state = _state(snapshot)
    assert (
        await newly_installed_skills(
            state, _runtime(ops_pool=None, database_gate=database_gate), _CONFIG
        )
        is None
    )  # type: ignore[arg-type]


# ── narrowed agents ──


async def test_narrowing_holds_and_a_configured_name_that_arrives_late_drifts_in(
    skills_dir: Path, database_gate: ProcessDbGate, aops_pool: AsyncConnectionPool
) -> None:
    """An agent narrowed to a specific list gets the same diff with no special
    case: a name that resolved to nothing at build time and resolves now is
    drift, and a skill outside the list never becomes drift."""
    pins = {"skills_to_inject_into_system_prompt": ["beta"]}
    _install(skills_dir, "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers(skill_view(_slices(pins).prompt))
    assert snapshot == set()  # `beta` is configured but not installed yet

    _install(skills_dir, "beta", "Beta desc")
    _install(skills_dir, "gamma", "Gamma desc")

    update = await _run_hook(snapshot, database_gate, aops_pool, pins=pins)

    assert update is not None
    (note,) = update["messages"]
    assert "meiva.skills.beta" in note.content  # pyright: ignore[reportUnknownMemberType]
    assert "gamma" not in note.content  # pyright: ignore[reportUnknownMemberType]
    assert update["capabilities"].indexed == {"beta"}  # pyright: ignore[reportUnknownMemberType]


async def test_sdk_disabled_skills_never_drift(
    skills_dir: Path, database_gate: ProcessDbGate, aops_pool: AsyncConnectionPool
) -> None:
    """`MEIVA_SDK_DISABLE=skills` removed the surface on purpose; an install must
    not put it back through the note."""
    pins = {"sdk_disable": ["skills"]}
    _install(skills_dir, "alpha", "Alpha desc")
    assert await _run_hook(set(), database_gate, aops_pool, pins=pins) is None


async def test_a_stale_config_name_warns_on_each_resolution(
    skills_dir: Path,
    caplog: pytest.LogCaptureFixture,
    database_gate: ProcessDbGate,
    aops_pool: AsyncConnectionPool,
) -> None:
    """Each unresolved resolution remains visible without global suppression."""
    pins = {"skills_to_inject_into_system_prompt": ["does-not-exist"]}
    _install(skills_dir, "alpha", "Alpha desc")

    with caplog.at_level("WARNING"):
        for _ in range(3):
            await _run_hook(set(), database_gate, aops_pool, pins=pins)

    assert caplog.text.count("does-not-exist") == 3
    assert "skills_to_inject_into_system_prompt" in caplog.text


# ── registration ──


def test_the_drift_hook_is_a_framework_before_llm_hook_run_last() -> None:
    """Framework-owned and unconditional — the index under-reporting is a
    correctness problem, not a plugin's optional layer. It runs last at the edge, after repair's
    guard and compact's possible full replacement."""
    assert framework_hooks()["before_llm"][-1] is newly_installed_skills
