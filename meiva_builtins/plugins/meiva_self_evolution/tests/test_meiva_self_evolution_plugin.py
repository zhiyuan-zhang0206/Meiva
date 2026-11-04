from __future__ import annotations

from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from agent.graph.hooks.compact import compact_reminder
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths import meiva_home
from base.paths.home import resolve_meiva_home
from meiva_builtins.plugins.meiva_self_evolution import agent_runtime
from meiva_builtins.plugins.meiva_self_evolution.agent_runtime import TO_DO_BETTER_FILENAME


def _slices(*, workspace_in_system_prompt: bool = True) -> AgentSlices:
    return AgentSlices.resolve(
        {
            "llm_model": "deepseek-flash",
            "compact_reminder_fraction": 0.000001,
            "auto_compact_fraction": 0.4,
            "auto_compact_ceiling_tokens": 0,
        },
        default_reader=defaults(agent__workspace_in_system_prompt=workspace_in_system_prompt),
    )


def _runtime(slices: AgentSlices) -> Runtime[MeivaContext]:
    return Runtime(
        context=MeivaContext(
            agent=slices,
            catalog=build_model_catalog(home=meiva_home()),
            extensions=ExtensionRegistry((("meiva_self_evolution", agent_runtime.contribute()),)),
            clock_factory=Clock.from_settings,
            meiva_home=resolve_meiva_home(),
        )
    )


# meiva:note
# The compact-reminder hook returns `dict[str, Any]` in `agent`; taking it as `object` and narrowing
# keeps the `Any` out of the test, and the single injected note is narrowed to its text.
def _note_text(update: object) -> str:
    assert is_object_dict(update)
    messages = update["messages"]
    assert is_object_list(messages)
    [note] = messages
    assert isinstance(note, HumanMessage)
    assert isinstance(note.content, str)
    return note.content


def test_reminder_names_the_file_and_stays_optional() -> None:
    text = agent_runtime.self_evolution_compact_reminder(
        _slices(), catalog=build_model_catalog(home=meiva_home())
    )

    lowered = text.lower()
    assert TO_DO_BETTER_FILENAME in text
    assert "skip it when nothing qualifies" in lowered
    for mandatory in ("must ", "always ", "every window", "required"):
        assert mandatory not in lowered


def test_reminder_is_silent_when_the_workspace_is_not_in_the_prompt() -> None:
    text = agent_runtime.self_evolution_compact_reminder(
        _slices(workspace_in_system_prompt=False),
        catalog=build_model_catalog(home=meiva_home()),
    )

    assert text == ""


async def test_compact_reminder_carries_the_plugin_section() -> None:
    state = AgentState(messages=[HumanMessage(content="history " * 200)])
    text = _note_text(await compact_reminder(state, _runtime(_slices()), {}))

    assert "[meiva_self_evolution]" in text
    assert TO_DO_BETTER_FILENAME in text


async def test_compact_reminder_omits_the_section_without_a_workspace_path() -> None:
    state = AgentState(messages=[HumanMessage(content="history " * 200)])
    text = _note_text(
        await compact_reminder(state, _runtime(_slices(workspace_in_system_prompt=False)), {})
    )

    assert "[meiva_self_evolution]" not in text


async def test_after_init_seeds_a_missing_file_and_keeps_an_existing_one(
    unit_home: Path,
) -> None:
    hook = agent_runtime.SeedToDoBetterHook()
    runtime = Runtime(
        context=MeivaContext(
            identity=AgentIdentity(agent_id=11, owns_loop=True), meiva_home=resolve_meiva_home()
        )
    )
    path = unit_home / "workspaces" / "11" / TO_DO_BETTER_FILENAME

    assert await hook(AgentState(), runtime, {}) is None
    assert path.is_file()

    path.write_text("Lesson: check the failing shard first.", encoding="utf-8")
    await hook(AgentState(), runtime, {})
    assert path.read_text(encoding="utf-8") == "Lesson: check the failing shard first."


async def test_after_init_without_an_identity_writes_nothing(unit_home: Path) -> None:
    hook = agent_runtime.SeedToDoBetterHook()

    assert (
        await hook(AgentState(), Runtime(context=MeivaContext(meiva_home=resolve_meiva_home())), {})
        is None
    )
    assert not (unit_home / "workspaces").exists()
