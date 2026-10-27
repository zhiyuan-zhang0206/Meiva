# meiva:note
# The execute-code-timeout context note.
#
# Policy: the hard wall-clock bound on `execute_code` is declared once per window,
# because the failure it prevents — a call silently killed mid-work — is expensive
# to discover from a traceback alone. The note renders the value the turn's frozen
# agent configuration carries, and must resolve the HOSTED turn identity carried by
# the explicit host context.

from __future__ import annotations

from agent.graph.prompt.context_notes import exec_timeout_note
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.agents.messages.kwargs import NoteTag
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home


def _context(
    agent_id: int | None = 7, *, exec_timeout_seconds: float | None = None
) -> MeivaContext:
    pins = {} if exec_timeout_seconds is None else {"exec_timeout_seconds": exec_timeout_seconds}
    return MeivaContext(
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True) if agent_id is not None else None,
        agent=AgentSlices.resolve(pins, default_reader=defaults()),
        catalog=build_model_catalog(home=meiva_home()),
        clock_factory=Clock.from_settings,
    )


# meiva:note
# The value comes from the turn's configuration, not a hard-coded constant.
def test_declares_the_configured_timeout() -> None:
    note = exec_timeout_note(_context(exec_timeout_seconds=600.0))
    assert note is not None
    content = str(note.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert "600 seconds (10 minutes)" in content
    assert "it will be killed" in content
    assert note.additional_kwargs["meiva_note_tag"] == NoteTag.EXEC_TIMEOUT  # pyright: ignore[reportUnknownMemberType]


def test_opts_out_without_an_agent_identity() -> None:
    assert exec_timeout_note(_context(None)) is None


def test_renders_under_a_hosted_turn_identity() -> None:
    """The explicit host context works without binding the shared SDK."""
    note = exec_timeout_note(_context(29))

    assert note is not None
    assert "hard wall-clock timeout" in str(note.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
