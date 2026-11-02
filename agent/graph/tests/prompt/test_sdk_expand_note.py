# meiva:note
# The note that tells an agent its own SDK-expand configuration named entries this window's SDK
# reference could not carry. Until #6027 the only trace was a host-side warning: the agent whose
# prompt lost a section could not learn it, correct it, or open the member on demand.
from __future__ import annotations

from agent.graph.prompt.context_notes import sdk_expand_note
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.agents.messages.kwargs import NoteTag
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home


def _context(expand: list[str], *, disable: list[str] | None = None) -> MeivaContext:
    return MeivaContext(
        identity=AgentIdentity(agent_id=7, owns_loop=True),
        agent=AgentSlices.resolve(
            {
                "sdk_expand_in_system_prompt": expand,
                "sdk_disable": disable or [],
            },
            default_reader=defaults(),
        ),
        catalog=build_model_catalog(home=meiva_home()),
        clock_factory=Clock.from_settings,
    )


# meiva:note
# One entry that renders (a namespace), one `meiva.help` documents no value of (the live context),
# one whose namespace exists but whose member does not: the note names the two that will not render,
# under the path the configuration wrote, and leaves the one that rendered out of the list.
def test_names_each_entry_that_will_not_render() -> None:
    note = sdk_expand_note(_context(["shell", "context", "shell.no_such_member"]))

    assert note is not None
    content = str(note.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert "meiva.context" in content
    assert "meiva.shell.no_such_member" in content
    assert "- meiva.shell:" not in content
    assert note.additional_kwargs["meiva_note_tag"] == NoteTag.SDK_EXPAND  # pyright: ignore[reportUnknownMemberType]


# meiva:note
# The reverse control: a configuration whose entries all render produces no note, or every agent
# reads a "something is broken" line it can do nothing about. The entries are the core namespaces,
# because a plugin namespace (`tasks` is one) is legitimately absent from a process that loads no
# plugins, and the note reports that absence rather than hiding it.
def test_nothing_to_say_when_every_entry_renders() -> None:
    assert sdk_expand_note(_context(["shell", "files", "agents"])) is None


# meiva:note
# An entry named in `MEIVA_SDK_DISABLE` was dropped on purpose, so it is not a broken entry and
# must not appear in the note — the same filter the reference itself applies before rendering.
def test_a_deliberately_disabled_entry_is_not_reported() -> None:
    assert sdk_expand_note(_context(["shell", "context"], disable=["context"])) is None


# meiva:note
# A namespace this process does not serve at all — the shape of a plugin that is not enabled here,
# which is indistinguishable from a typo — is not reported to the agent. A note that fired for it
# would fire in every window of every agent whose cluster runs fewer plugins than its configuration
# names; the reference's own warning keeps the forensic trace, with both causes in its text.
def test_an_entry_this_process_cannot_serve_is_not_reported() -> None:
    assert sdk_expand_note(_context(["no_such_namespace_anywhere"])) is None
