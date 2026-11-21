import os
from typing import cast

import pytest

from agent.graph.prompt.conversation import (
    delivery_style_prompt,
    harness_user_reply_section,
    user_reply_section,
)
from agent.graph.prompt.live import LiveSdk, SectionInputs
from agent.graph.prompt.skill_selection import SkillView
from agent.graph.prompt.system_prompt import build_harness_system_prompt, build_system_prompt
from base.agents.messages.reply_intent import DISPLAY_TO_USER, HIDE_FROM_USER
from base.api_contracts.agent_contract import Harness
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import EMPTY
from base.paths import meiva_home

ENVIRON = dict(os.environ)


def _slices(**changes: object) -> AgentSlices:
    return AgentSlices.resolve(default_reader=defaults(**changes))


def _inputs(slices: AgentSlices, *, harness: Harness | None = None) -> SectionInputs:
    return SectionInputs(
        slices,
        build_model_catalog(home=meiva_home()),
        LiveSdk(SkillView((), (), (), ()), (), ()),
        harness=harness,
    )


@pytest.mark.parametrize("fast_acknowledgement", [False, True])
@pytest.mark.parametrize("style", ["off", "oriented", "concise", "silent"])
def test_initial_human_response_is_present_without_plugins(
    style: str, fast_acknowledgement: bool
) -> None:
    slices = _slices(
        agent__agent_communication_style=style,
        agent__fast_acknowledgement=fast_acknowledgement,
    )

    prompt = build_system_prompt(
        EMPTY,
        slices,
        agent_id=1,
        catalog=build_model_catalog(home=meiva_home()),
        home=meiva_home(),
        environ=ENVIRON,
    )

    assert prompt.count(user_reply_section(_inputs(slices))) == 1
    assert "no separate acknowledgment is needed. Do the work and deliver the result" in prompt
    assert "## Fast acknowledgement" in prompt
    assert "deliver the result in that conversation" in prompt
    assert "Peer messages, watcher events, scheduled wakes and system notes" in prompt
    assert "do not require courtesy replies" in prompt
    if style == "silent":
        expected = (
            "Work without narrating after any opening confirmation"
            if fast_acknowledgement
            else "Work without narrating. While a task"
        )
        assert expected in prompt
        assert "Silence here costs the user nothing" not in prompt
    if style != "off":
        assert "Ordinary information creates no question or notification" in prompt


@pytest.mark.parametrize("style", ["off", "oriented", "concise", "silent"])
def test_reply_routing_and_handoff_policy_survive_every_style(style: str) -> None:
    slices = _slices(agent__agent_communication_style=style)
    prompt = build_system_prompt(
        EMPTY,
        slices,
        agent_id=1,
        catalog=build_model_catalog(home=meiva_home()),
        home=meiva_home(),
        environ=ENVIRON,
    )

    assert prompt.count("Reply in ordinary assistant text in this conversation") == 1
    assert "tool call follows the same visibility rule as a standalone reply" in prompt
    assert "No separate SDK call is needed" in prompt
    assert "when resuming unfinished human requests from a handoff" in prompt
    assert "further investigation should address a concrete remaining question" in prompt
    assert "rather than inventing additional ones" in prompt
    assert "meiva.ui.notify" not in user_reply_section(_inputs(slices))


@pytest.fixture(
    params=[
        (style, shape)
        for style in ["off", "oriented", "concise", "silent"]
        for shape in [False, True]
    ]
)
def communication_prompt(request: pytest.FixtureRequest) -> tuple[str, str]:
    style, output_shape = cast(tuple[str, bool], request.param)
    slices = _slices(
        agent__agent_communication_style=style,
        agent__prompt_output_conciseness_enabled=output_shape,
    )

    return style, build_system_prompt(
        EMPTY,
        slices,
        agent_id=1,
        catalog=build_model_catalog(home=meiva_home()),
        home=meiva_home(),
        environ=ENVIRON,
    )


def test_language_policy_survives_optional_guidance(communication_prompt: tuple[str, str]) -> None:
    _, prompt = communication_prompt

    assert prompt.count("## Communication language") == 1
    assert "Follow the current request first, then standing user preferences" in prompt
    assert "injected memory or project instructions" in prompt
    assert "English prompts, code, logs and peer messages do not reset that preference" in prompt
    assert "Preserve code, identifiers, quotations and requested artifacts" in prompt


def test_user_facing_text_policy_survives_every_style(
    communication_prompt: tuple[str, str],
) -> None:
    style, prompt = communication_prompt

    assert prompt.count("## Meaningful progress") == 1
    assert "Treat visible assistant text as substantive communication with the user" in prompt
    assert "Every message must be independently useful to the user" in prompt
    assert "without watching tool execution or reconstructing your internal steps" in prompt
    assert "Make it self-contained: state what changed, why it matters" in prompt
    assert ("This agent is configured for live pairing" in prompt) == (style == "oriented")
    assert "every message must remain self-contained and useful to the user" in prompt
    assert "Do not assume continuous attention to the session" in prompt
    assert "after an hour away" not in prompt


def test_milestone_policy_preserves_results_and_questions(
    communication_prompt: tuple[str, str],
) -> None:
    _, prompt = communication_prompt

    assert "meaningful milestones for the user: a usable result" in prompt
    assert "Completing an internal step or ending a turn is not itself a milestone" in prompt
    assert "Call `execute_code` with no accompanying assistant text on intermediate turns" in prompt
    assert "Do not narrate routine tool calls, retries, file reads, internal bookkeeping" in prompt
    assert "There is no fixed reporting cadence" in prompt
    assert "without a tool-call preamble or trailing introduction" in prompt
    assert "Direct answers, necessary questions" in prompt
    assert "omitting evidence needed to use the result" in prompt
    assert "do not require courtesy replies or automatic closing summaries" in prompt
    assert "emit alongside each action" not in prompt


def test_explicit_visibility_guidance_in_native_and_harness_sections() -> None:
    inputs = _inputs(_slices())
    for section in (user_reply_section(inputs), harness_user_reply_section(inputs)):
        assert "## Output visibility" in section
        assert DISPLAY_TO_USER in section
        assert HIDE_FROM_USER in section
        assert "folded into Details, which the user can expand and read" in section
        assert "human and background wakes alike" in section


@pytest.mark.parametrize("style", ["off", "oriented", "concise", "silent"])
def test_sdk_prompt_has_one_delivery_protocol(style: str) -> None:
    slices = _slices(agent__user_delivery_mode="sdk", agent__agent_communication_style=style)
    prompt = build_system_prompt(
        EMPTY,
        slices,
        agent_id=1,
        catalog=build_model_catalog(home=meiva_home()),
        home=meiva_home(),
        environ=ENVIRON,
    )
    assert "meiva.ui.publish" in prompt
    assert "meiva.ui.ask" in prompt
    assert "Publishing or asking never ends execution" in prompt
    assert "Suggestions are optional candidates" in prompt
    assert "Every question has a user-editable Note" in prompt
    assert "Details which the user can expand" in prompt
    assert "meiva.ui.notify" not in prompt
    assert "notification feed" not in prompt
    assert DISPLAY_TO_USER not in prompt
    assert "Reply in ordinary assistant text" not in prompt
    assert "meiva.ui.publish" in harness_user_reply_section(_inputs(slices))


def test_naive_prompt_keeps_plain_assistant_delivery() -> None:
    text = user_reply_section(_inputs(_slices(agent__user_delivery_mode="naive")))
    assert "Reply in ordinary assistant text" in text
    assert DISPLAY_TO_USER not in text and HIDE_FROM_USER not in text


_RETIRED_OPENING_REPLY_PHRASES = (
    "Whenever your first",
    "Never emit a tool-only first turn",
    "Do not leave the user waiting silently",
    "empty receipt acknowledgment",
    "initial-response rule",
    "After the opening reply",
    "required opening reply",
    "the substantive opening reply",
    "required initial reply",
    "short substantive opening",
)


def _rendered(mode: str, *, fast_acknowledgement: bool) -> dict[str, str]:
    slices = _slices(
        agent__user_delivery_mode=mode, agent__fast_acknowledgement=fast_acknowledgement
    )
    inputs = _inputs(slices)
    rendered = {
        f"native/{mode}": user_reply_section(inputs),
        f"harness/{mode}": harness_user_reply_section(inputs),
    }
    for style in ("oriented", "concise", "silent"):
        rendered[f"style/{style}/{mode}"] = delivery_style_prompt(
            mode, style, fast_acknowledgement=fast_acknowledgement
        )
    return rendered


@pytest.mark.parametrize("fast_acknowledgement", [False, True])
@pytest.mark.parametrize("mode", ["prefix", "naive"])
def test_retired_opening_reply_phrases_absent_in_text_modes(
    mode: str, fast_acknowledgement: bool
) -> None:
    rendered = _rendered(mode, fast_acknowledgement=fast_acknowledgement)
    for key, text in rendered.items():
        assert not any(phrase in text for phrase in _RETIRED_OPENING_REPLY_PHRASES), key
    native = rendered[f"native/{mode}"]
    assert "no separate acknowledgment is needed. Do the work and deliver the result" in native
    assert "Each mid-task progress update must stand alone" in native
    assert "Call `execute_code` with no accompanying" in native
    assert "Direct answers, necessary questions and complete final deliverables" in native
    assert "respond promptly" in native


@pytest.mark.parametrize("fast_acknowledgement", [False, True])
def test_retired_opening_reply_phrases_absent_in_sdk_mode(fast_acknowledgement: bool) -> None:
    rendered = _rendered("sdk", fast_acknowledgement=fast_acknowledgement)
    for key, text in rendered.items():
        assert not any(phrase in text for phrase in _RETIRED_OPENING_REPLY_PHRASES), key
    assert "Publish only information that passes the user-attention test" in rendered["native/sdk"]


@pytest.mark.parametrize("fast_acknowledgement", [False, True])
def test_attention_policy_is_shared_across_delivery_modes_and_callers(
    fast_acknowledgement: bool,
) -> None:
    sections = [
        text.split("## User attention\n\n", 1)[1].split("\n\n#", 1)[0]
        for mode in ("prefix", "sdk", "naive")
        for key, text in _rendered(mode, fast_acknowledgement=fast_acknowledgement).items()
        if key.startswith(("native/", "harness/"))
    ]
    assert len(sections) == 6
    assert all(section == sections[0] for section in sections)
    headers = {
        "prefix": "## Output visibility",
        "sdk": "# User delivery",
        "naive": "## Delivery mechanics",
    }
    for mode, header in headers.items():
        rendered = _rendered(mode, fast_acknowledgement=fast_acknowledgement)
        for prefix in ("native", "harness"):
            text = rendered[f"{prefix}/{mode}"]
            assert text.index("## User attention") > text.index(header)


@pytest.mark.parametrize("fast_acknowledgement", [False, True])
def test_claude_code_handoff_appends_visibility_why_only_in_prefix_mode(
    fast_acknowledgement: bool,
) -> None:
    prefix_slices = _slices(
        agent__user_delivery_mode="prefix", agent__fast_acknowledgement=fast_acknowledgement
    )
    claude_text = harness_user_reply_section(_inputs(prefix_slices, harness=Harness.CLAUDE_CODE))
    baseline_harness = harness_user_reply_section(_inputs(prefix_slices))
    baseline_native = user_reply_section(_inputs(prefix_slices))
    visibility_block = claude_text.split("## Output visibility\n\n", 1)[1].split(
        "\n\n## User attention", 1
    )[0]

    assert "Unlike standalone Claude Code" in visibility_block
    assert "meiva.agents.send_message" in visibility_block
    assert "When you have a delegator" in visibility_block
    assert "unexpected unread badge" in visibility_block
    assert "delegated summaries" in visibility_block
    assert "MEIVA_HIDE_FROM_USER" in visibility_block
    assert "MEIVA_DISPLAY_TO_USER" in visibility_block
    assert (
        claude_text.split("## User attention\n\n", 1)[1]
        == baseline_harness.split("## User attention\n\n", 1)[1]
    )

    for other in (Harness.CODEX, Harness.ANTIGRAVITY, Harness.MEIVA, None):
        assert harness_user_reply_section(_inputs(prefix_slices, harness=other)) == baseline_harness
    assert (
        user_reply_section(_inputs(prefix_slices, harness=Harness.CLAUDE_CODE)) == baseline_native
    )

    for mode in ("sdk", "naive"):
        mode_slices = _slices(
            agent__user_delivery_mode=mode, agent__fast_acknowledgement=fast_acknowledgement
        )
        assert harness_user_reply_section(
            _inputs(mode_slices, harness=Harness.CLAUDE_CODE)
        ) == harness_user_reply_section(_inputs(mode_slices))


def test_build_harness_system_prompt_leaves_non_claude_harnesses_unchanged() -> None:
    slices = _slices()
    catalog = build_model_catalog(home=meiva_home())
    baseline = build_harness_system_prompt(
        EMPTY, slices, agent_id=1, catalog=catalog, home=meiva_home(), environ=ENVIRON
    )
    for other in (Harness.CODEX, Harness.ANTIGRAVITY):
        assert (
            build_harness_system_prompt(
                EMPTY,
                slices,
                agent_id=1,
                catalog=catalog,
                home=meiva_home(),
                environ=ENVIRON,
                harness=other,
            )
            == baseline
        )
    claude = build_harness_system_prompt(
        EMPTY,
        slices,
        agent_id=1,
        catalog=catalog,
        home=meiva_home(),
        environ=ENVIRON,
        harness=Harness.CLAUDE_CODE,
    )
    assert "Unlike standalone Claude Code" in claude
    assert "Unlike standalone Claude Code" not in baseline


_FAST_ACK_MODE_PHRASES: dict[str, tuple[str, str, str]] = {
    "prefix": (
        f"emit one brief `{DISPLAY_TO_USER}`",
        f"use `{HIDE_FROM_USER}` for all tool calls",
        f"only then use `{DISPLAY_TO_USER}`",
    ),
    "sdk": (
        "call `meiva.ui.publish(...)` with one concise sentence",
        "end with `meiva.ui.publish(None)`",
        "Do not call `meiva.ui.publish(...)` just to send an empty",
    ),
    "naive": (
        "emit one brief confirmation sentence",
        "avoid a redundant closing summary",
        "Do not send empty or low-information acknowledgements",
    ),
}


@pytest.mark.parametrize("mode", ["prefix", "sdk", "naive"])
def test_fast_acknowledgement_off_and_on_across_modes_and_callers(mode: str) -> None:
    off_slices = _slices(agent__user_delivery_mode=mode, agent__fast_acknowledgement=False)
    on_slices = _slices(agent__user_delivery_mode=mode, agent__fast_acknowledgement=True)
    off_native = user_reply_section(_inputs(off_slices))
    off_harness = harness_user_reply_section(_inputs(off_slices))
    on_native = user_reply_section(_inputs(on_slices))
    on_harness = harness_user_reply_section(_inputs(on_slices))

    assert (
        off_native.split("## Fast acknowledgement", 1)[1]
        == off_harness.split("## Fast acknowledgement", 1)[1]
    )
    assert (
        on_native.split("## Fast acknowledgement", 1)[1]
        == on_harness.split("## Fast acknowledgement", 1)[1]
    )
    assert off_native != on_native
    assert off_harness != on_harness

    on_phrase_1, on_phrase_2, off_phrase = _FAST_ACK_MODE_PHRASES[mode]
    assert on_phrase_1 in on_native and on_phrase_2 in on_native
    assert off_phrase in off_native


@pytest.mark.parametrize("mode", ["prefix", "sdk", "naive"])
def test_fast_acknowledgement_is_only_for_direct_human_requests(mode: str) -> None:
    for key, text in _rendered(mode, fast_acknowledgement=True).items():
        if key.startswith("style/"):
            continue
        fast_ack = text.split("## Fast acknowledgement", 1)[1]
        assert (
            "Only a new request sent directly by the human in this conversation qualifies"
            in fast_ack
        )
        assert (
            "agent briefs, relays and peer messages do not, even when they quote the human"
            in fast_ack
        )
        assert "no human is waiting for receipt of an agent brief" in fast_ack

    for key, text in _rendered(mode, fast_acknowledgement=False).items():
        if key.startswith("style/"):
            continue
        fast_ack = text.split("## Fast acknowledgement", 1)[1]
        assert "Agent briefs, relays and peer messages need no acknowledgement" in fast_ack
        assert "even when they quote the human; follow delegated reporting instead" in fast_ack
