from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.agents.messages.kwargs import read_meiva_kwargs
from base.clock import Clock
from base.cluster.machine import MachineIdentityReader
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths import meiva_home
from meiva.tests.fixtures.gateway_wire import GatewayStub, GatewayWire
from meiva_builtins.plugins.meiva_memory import inherit
from meiva_builtins.plugins.meiva_memory.inherited_blocks import (
    INHERITABLE_CLOSE,
    INHERITABLE_OPEN,
    Ancestor,
    EntryBlocks,
    SizeCaps,
    parse_inheritable_blocks,
    render_inherited,
    split_ancestors,
)

OPEN = INHERITABLE_OPEN
CLOSE = INHERITABLE_CLOSE

pytestmark = pytest.mark.usefixtures("unit_home")


def _context(
    clients: ClientSet | None,
    machine_identity: MachineIdentityReader,
    agent_id: int | None = 1,
    **pins: Any,
) -> MeivaContext:
    return MeivaContext(
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True) if agent_id is not None else None,
        agent=AgentSlices.resolve(pins, default_reader=defaults()),
        clock_factory=Clock.from_settings,
        machine=machine_identity,
        clients=clients if clients is not None else ClientSet(),
    )


def _wrap(*blocks: str) -> str:
    return "\n\n".join(f"{OPEN}\n{block}\n{CLOSE}" for block in blocks)


def _note_text(note: HumanMessage) -> str:
    return note.text


def _note_tag(note: HumanMessage) -> object:
    return read_meiva_kwargs(note).get("meiva_note_tag")


def _write_entry(agent_id: int, slug: str, body: str) -> Path:
    mem = meiva_home() / "workspaces" / str(agent_id) / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    path = mem / f"{slug}.md"
    path.write_text(body, encoding="utf-8")
    return path


def _row(agent_id: int, machine: str, label: str | None = None, depth: int = 1) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "label": label,
        "status": "idling",
        "machine": machine,
        "depth": depth,
    }


def _chain(wire: GatewayWire, agent_id: int, *rows: dict[str, Any]) -> None:
    wire.stub.reply("GET", f"/api/agents/{agent_id}/born-chain", {"ancestors": list(rows)})


def _entries(*texts: str, agent_id: int = 7, label: str | None = None) -> list[EntryBlocks]:
    ancestor = Ancestor(agent_id=agent_id, label=label, machine="here")
    return [
        EntryBlocks(
            ancestor=ancestor,
            entry_name=f"entry-{index}.md",
            entry_path=f"/home/{agent_id}/memory/entry-{index}.md",
            blocks=(text,),
        )
        for index, text in enumerate(texts)
    ]


def test_parser_reads_a_single_block() -> None:
    text = f"intro\n{OPEN}\nthe rule\n{CLOSE}\noutro"
    assert parse_inheritable_blocks(text).blocks == ("the rule",)


def test_parser_reads_multiple_blocks_in_order() -> None:
    assert parse_inheritable_blocks(_wrap("A", "B")).blocks == ("A", "B")


def test_parser_skips_frontmatter_and_takes_only_the_body() -> None:
    text = f"---\nname: x\ndescription: {OPEN}\nnot a block\n---\n\n{OPEN}\nreal\n{CLOSE}"
    assert parse_inheritable_blocks(text).blocks == ("real",)


def test_parser_normalizes_crlf() -> None:
    text = f"---\r\nname: x\r\n---\r\n\r\n{OPEN}\r\ncrlf block\r\n{CLOSE}"
    assert parse_inheritable_blocks(text).blocks == ("crlf block",)


def test_parser_drops_malformed_fences_with_a_warning() -> None:
    unclosed = parse_inheritable_blocks(f"{OPEN}\nsecret\nstill open")
    assert unclosed.blocks == ()
    assert unclosed.warnings == ("unclosed open fence — block dropped",)

    stray_close = parse_inheritable_blocks(f"body\n{CLOSE}\nmore")
    assert stray_close.blocks == ()
    assert stray_close.warnings == ("close fence without an open ignored",)

    nested = parse_inheritable_blocks(f"{OPEN}\nouter\n{OPEN}\ninner\n{CLOSE}")
    assert nested.blocks == ("outer\ninner",)
    assert nested.warnings == ("nested open fence ignored",)

    assert parse_inheritable_blocks(f"{OPEN}\n{CLOSE}").blocks == ()


def test_depth_slices_the_chain_and_remote_ancestors_are_set_apart() -> None:
    chain = [
        Ancestor(1, "Parent", "here"),
        Ancestor(2, None, "elsewhere"),
        Ancestor(3, None, "here"),
    ]

    shallow = split_ancestors(chain, depth=1, this_machine="here")
    assert [row.agent_id for row in shallow.local] == [1]
    assert shallow.remote == ()

    deep = split_ancestors(chain, depth=3, this_machine="here")
    assert [row.agent_id for row in deep.local] == [1, 3]
    assert [row.agent_id for row in deep.remote] == [2]


_NO_CAPS = SizeCaps(block_chars=0, total_chars=0)


def test_nothing_declared_renders_nothing() -> None:
    assert render_inherited([], [], depth=1, caps=_NO_CAPS) is None
    empty = EntryBlocks(Ancestor(7, None, "here"), "e.md", "/e.md", ())
    assert render_inherited([empty], [], depth=1, caps=_NO_CAPS) is None


def test_a_section_names_the_ancestor_and_joins_one_entrys_fences() -> None:
    entry = EntryBlocks(
        Ancestor(7, "Parent", "here"), "rules.md", "/rules.md", ("block one", "block two")
    )

    rendered = render_inherited([entry], [], depth=2, caps=_NO_CAPS)

    assert rendered is not None
    assert rendered.text.startswith(
        "Memory your ancestors chose to share with descendants (depth 2,"
    )
    assert "## ancestor #7 (Parent) — memory/rules.md\n\nblock one\n\nblock two" in rendered.text
    assert (rendered.block_count, rendered.section_count, rendered.warnings) == (2, 1, ())


def test_remote_ancestors_are_named_in_the_footer() -> None:
    rendered = render_inherited(
        _entries("local block"),
        [Ancestor(9, None, "test-elsewhere"), Ancestor(10, None, None)],
        depth=2,
        caps=_NO_CAPS,
    )

    assert rendered is not None
    assert rendered.text.endswith(
        "---\nAncestors on other machines (not readable from here): "
        "#9 (test-elsewhere), #10 (unknown machine)."
    )


def test_block_guardrail_truncates_with_a_visible_marker() -> None:
    rendered = render_inherited(
        _entries("x" * 200), [], depth=1, caps=SizeCaps(block_chars=50, total_chars=0)
    )

    assert rendered is not None
    assert "[truncated at 50 chars — full entry: /home/7/memory/entry-0.md]" in rendered.text
    assert "x" * 51 not in rendered.text
    assert rendered.warnings == (
        "block in /home/7/memory/entry-0.md is 200 chars — truncated to 50",
    )


def test_total_guardrail_clips_the_crossing_section_and_omits_the_rest() -> None:
    rendered = render_inherited(
        _entries("a" * 40, "b" * 40), [], depth=1, caps=SizeCaps(block_chars=0, total_chars=60)
    )

    assert rendered is not None
    assert "a" * 40 in rendered.text
    assert "b" * 20 in rendered.text
    assert "b" * 21 not in rendered.text
    assert "[truncated: the 60-char inherited-memory budget is reached" in rendered.text
    assert "further blocks omitted" in rendered.text
    assert rendered.warnings == (
        "total budget of 60 chars reached at /home/7/memory/entry-1.md — truncated",
    )


def test_exact_exhaustion_omits_the_crossing_section_whole() -> None:
    rendered = render_inherited(
        _entries("a" * 40, "b" * 40), [], depth=1, caps=SizeCaps(block_chars=0, total_chars=40)
    )

    assert rendered is not None
    assert "a" * 40 in rendered.text
    assert "b" * 10 not in rendered.text
    assert "memory/entry-1.md" not in rendered.text
    assert rendered.text.count("## ancestor") == 1
    assert "[truncated: the 40-char inherited-memory budget is reached" in rendered.text


def test_guardrails_can_be_disabled() -> None:
    rendered = render_inherited(_entries("y" * 200), [], depth=1, caps=_NO_CAPS)

    assert rendered is not None
    assert "y" * 200 in rendered.text
    assert "truncated" not in rendered.text
    assert rendered.warnings == ()


def test_a_layer_that_is_off_reads_nothing(
    wire: GatewayWire, machine_identity: MachineIdentityReader
) -> None:
    _chain(wire, 1, _row(600301, machine_identity.name()))
    clients = wire.context.clients

    off = _context(clients, machine_identity, memory_inherit_depth=0)
    assert inherit.inherited_memory_note(off) is None

    isolated = _context(clients, machine_identity, memory_inherit_depth=1, eval_isolation=True)
    assert inherit.inherited_memory_note(isolated) is None

    assert wire.stub.received == []


def test_an_empty_chain_and_a_local_ancestor_without_a_store_yield_no_note(
    wire: GatewayWire, machine_identity: MachineIdentityReader
) -> None:
    ctx = _context(wire.context.clients, machine_identity)

    _chain(wire, 1)
    assert inherit.inherited_memory_note(ctx) is None

    _chain(wire, 1, _row(600301, machine_identity.name()))
    assert inherit.inherited_memory_note(ctx) is None


def test_direct_parent_blocks_are_injected(
    wire: GatewayWire, machine_identity: MachineIdentityReader
) -> None:
    _write_entry(600311, "family-rules", f"private\n\n{_wrap('Report in Chinese.')}\n")
    _chain(wire, 1, _row(600311, machine_identity.name(), label="Parent"))

    note = inherit.inherited_memory_note(_context(wire.context.clients, machine_identity))

    assert note is not None
    assert _note_tag(note) == "inherited_memory"
    content = _note_text(note)
    assert content.startswith("<system_note>\nMemory your ancestors chose to share")
    assert "verify facts that conflict with current evidence" in content
    assert "## ancestor #600311 (Parent) — memory/family-rules.md" in content
    assert "Report in Chinese." in content
    assert "private" not in content


def test_blocks_render_nearest_first_with_sorted_entries(
    wire: GatewayWire, machine_identity: MachineIdentityReader
) -> None:
    here = machine_identity.name()
    _write_entry(600321, "b-entry", _wrap("b block"))
    _write_entry(600321, "a-entry", _wrap("a block one", "a block two"))
    _write_entry(600322, "grandparent", _wrap("grandparent block"))
    _chain(wire, 1, _row(600321, here, label="Parent"), _row(600322, here, depth=2))

    note = inherit.inherited_memory_note(
        _context(wire.context.clients, machine_identity, memory_inherit_depth=2)
    )

    assert note is not None
    content = _note_text(note)
    assert content.index("#600321") < content.index("#600322")
    assert content.index("memory/a-entry.md") < content.index("memory/b-entry.md")
    assert "a block one\n\na block two" in content
    assert "grandparent block" in content


def test_remote_ancestors_are_skipped_and_footered(
    wire: GatewayWire, machine_identity: MachineIdentityReader
) -> None:
    _write_entry(600341, "local", _wrap("local block"))
    _chain(
        wire,
        1,
        _row(600342, "test-elsewhere"),
        _row(600341, machine_identity.name(), depth=2),
    )
    ctx = _context(wire.context.clients, machine_identity, memory_inherit_depth=2)

    note = inherit.inherited_memory_note(ctx)

    assert note is not None
    content = _note_text(note)
    assert "local block" in content
    assert (
        "Ancestors on other machines (not readable from here): #600342 (test-elsewhere)." in content
    )

    _chain(wire, 1, _row(600343, "test-elsewhere"))
    assert inherit.inherited_memory_note(ctx) is None


def test_an_unrouted_chain_read_degrades_and_is_not_cached(
    wire: GatewayWire, machine_identity: MachineIdentityReader
) -> None:
    ctx = _context(wire.context.clients, machine_identity)

    assert inherit.inherited_memory_note(ctx) is None
    assert inherit.inherited_memory_note(ctx) is None

    assert [r.path for r in wire.stub.received] == ["/api/agents/1/born-chain"] * 2


def test_a_refused_chain_read_degrades_and_is_not_cached(
    machine_identity: MachineIdentityReader,
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        refused = f"http://127.0.0.1:{probe.getsockname()[1]}"
    with GatewayWire.open(GatewayStub(url=refused)) as unreachable:
        ctx = _context(unreachable.context.clients, machine_identity)

        assert inherit.inherited_memory_note(ctx) is None
        assert inherit.inherited_memory_note(ctx) is None


def test_chain_is_read_at_each_establishment(
    wire: GatewayWire, machine_identity: MachineIdentityReader
) -> None:
    _write_entry(600351, "rules", _wrap("fresh block"))
    _chain(wire, 1, _row(600351, machine_identity.name()))
    ctx = _context(wire.context.clients, machine_identity)

    assert inherit.inherited_memory_note(ctx) is not None
    assert inherit.inherited_memory_note(ctx) is not None

    assert [r.path for r in wire.stub.received] == ["/api/agents/1/born-chain"] * 2


def test_content_is_deterministic(
    wire: GatewayWire, machine_identity: MachineIdentityReader
) -> None:
    _write_entry(600361, "rules", _wrap("stable block"))
    _chain(wire, 1, _row(600361, machine_identity.name()))
    ctx = _context(wire.context.clients, machine_identity)

    first = inherit.inherited_memory_note(ctx)
    second = inherit.inherited_memory_note(ctx)

    assert first is not None and second is not None
    assert _note_text(first) == _note_text(second)


def test_the_size_caps_come_from_the_agents_slices(
    wire: GatewayWire, machine_identity: MachineIdentityReader
) -> None:
    entry = _write_entry(600371, "big", _wrap("x" * 200))
    _chain(wire, 1, _row(600371, machine_identity.name()))

    note = inherit.inherited_memory_note(
        _context(wire.context.clients, machine_identity, memory_inherit_max_block_chars=50)
    )

    assert note is not None
    assert f"[truncated at 50 chars — full entry: {entry}]" in _note_text(note)


def test_the_fork_graft_builds_the_new_agents_own_chain(
    wire: GatewayWire, machine_identity: MachineIdentityReader
) -> None:
    from meiva_builtins.plugins.meiva_memory import agent_runtime

    _write_entry(600401, "rules", _wrap("fork chain block"))
    _chain(wire, 5, _row(600401, machine_identity.name(), label="Fork parent"))
    registry = ExtensionRegistry((("meiva_memory", agent_runtime.contribute()),))
    declared = [note for _plugin, note in registry.context_notes()]
    inherited = next(note for note in declared if note.build is inherit.inherited_memory_note)

    built = inherited.build(_context(wire.context.clients, machine_identity, agent_id=5))

    assert inherited.on_fork
    assert isinstance(built, HumanMessage)
    assert _note_tag(built) == "inherited_memory"
    assert "fork chain block" in _note_text(built)
