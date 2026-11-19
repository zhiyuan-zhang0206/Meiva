# meiva:note
# `meiva.memory.SHARED_PATH` + `PERSONAL_PATH` + `search` + `IndexerUnavailable` unit tests. The SDK
# calls the local gateway over HTTP and never imports the embedder or a vector store; the
# data-plane behaviour tests live in `gateway/tests/routers/test_memory_search.py`. This file
# only tests the SDK-gateway wire and the SHARED_PATH prefix conversion.

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

import httpx
import pytest

import meiva
from base.agents.context.gateway_client.memory_search import MemorySearchResult
from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner
from base.api_contracts.agent_contract import Harness
from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths import meiva_home, memory_dir, workspace_dir
from base.paths.home import resolve_meiva_home
from meiva.gateway_client import use_client
from meiva.sdk_surface import install
from meiva_builtins.plugins.meiva_memory import plugin as memory_plugin

# ── Plugin simulation: install the meiva_memory SDK surface ───────────────


@pytest.fixture(autouse=True)
def _install_memory_sdk() -> Iterator[None]:
    install.install(
        ExtensionRegistry((("meiva_memory", memory_plugin.contribute()),)),
        config_images=DiskPluginConfigImages(resolve_meiva_home()),
        sampling=fixed_sampling_owner(),
    )
    yield
    install.uninstall()


# meiva:note
# An agent builds absolute note paths as `SHARED_PATH / "xxx.md"`, so the constant is an absolute
# `pathlib.Path` and not a string.
def test_path_is_absolute() -> None:
    assert meiva.memory.SHARED_PATH.is_absolute()


def test_path_under_unit_home() -> None:
    # meiva:note
    # Shared notes are rooted beneath the unit's Vault checkout.
    assert meiva_home() / "vault" / "memory" == meiva.memory.SHARED_PATH


def test_path_is_path_object() -> None:
    assert isinstance(meiva.memory.SHARED_PATH, Path)


# ── PERSONAL_PATH constant ───────────────────────────────────────────────


def test_personal_path_is_the_memory_folder_of_the_agent_workspace() -> None:
    from base.paths import workspace_dir
    from meiva.tests.fixtures.pin_agent import pin_agent

    pin_agent(17)
    assert workspace_dir(17, home=meiva_home()) / "memory" == meiva.memory.PERSONAL_PATH
    assert isinstance(meiva.memory.PERSONAL_PATH, Path)
    assert "PERSONAL_PATH" in meiva.memory.__all_for_meiva__


def test_personal_path_needs_an_agent_identity() -> None:
    from meiva.tests.fixtures.pin_agent import pin_no_identity

    pin_no_identity()
    with pytest.raises(AttributeError, match="needs an established agent identity"):
        _ = meiva.memory.PERSONAL_PATH
    assert not hasattr(meiva.memory, "PERSONAL_PATH")


def test_there_is_no_memory_writer() -> None:
    """Notes are written with `meiva.files`; `meiva.memory` has no `write`."""
    assert "write" not in meiva.memory.__all_for_meiva__
    assert not hasattr(meiva.memory, "write")


@pytest.mark.parametrize("name", ["SHARED_PATH", "PERSONAL_PATH"])
def test_path_docs_carry_the_note_format(name: str) -> None:
    from meiva.tests.fixtures.pin_agent import pin_agent

    pin_agent(17)
    doc = {"SHARED_PATH": meiva.memory.SHARED_PATH, "PERSONAL_PATH": meiva.memory.PERSONAL_PATH}[
        name
    ].__doc__
    assert "pure Markdown" in doc
    assert "# <Title>" in doc
    assert "8000 characters" in doc
    assert "meiva.files.write" in doc


# ── search SDK layer (HTTP wire) ─────────────────────────────────────────


class _SearchWire(NamedTuple):
    requests: list[httpx.Request]
    results: list[MemorySearchResult]


@pytest.fixture
def search_wire() -> Iterator[_SearchWire]:
    wire = _SearchWire([], [])

    def respond(request: httpx.Request) -> httpx.Response:
        wire.requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/memory/search"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "path": hit.path,
                        "description": hit.description,
                        "tags": hit.tags,
                        "score": hit.score,
                        "chunk": hit.chunk,
                    }
                    for hit in wire.results
                ]
            },
        )

    with (
        httpx.Client(
            base_url="http://gateway.test", transport=httpx.MockTransport(respond)
        ) as client,
        use_client(client),
    ):
        yield wire


def test_search_forwards_query_and_k_to_gateway(search_wire: _SearchWire) -> None:
    """The SDK sends the query and result limit through the memory-search wire contract."""
    meiva.memory.search("test query", k=7)
    [request] = search_wire.requests
    assert json.loads(request.content) == {"query": "test query", "k": 7, "purpose": "search"}


def test_search_default_k_is_5(search_wire: _SearchWire) -> None:
    """`k` default = 5 (consistent with gateway endpoint schema)."""
    meiva.memory.search("q")
    [request] = search_wire.requests
    assert json.loads(request.content)["k"] == 5


def test_search_defaults_timeout_to_none(search_wire: _SearchWire) -> None:
    """No `timeout` leaves the gateway deadline unspecified on the wire."""
    meiva.memory.search("q")
    [request] = search_wire.requests
    assert "deadline_seconds" not in json.loads(request.content)


def test_search_forwards_timeout(search_wire: _SearchWire) -> None:
    """A caller-provided `timeout` reaches the gateway unmodified, with an HTTP margin."""
    meiva.memory.search("q", timeout=7.0)
    [request] = search_wire.requests
    assert json.loads(request.content)["deadline_seconds"] == 7.0
    assert request.extensions["timeout"]["read"] == 10.0


def test_search_rejects_non_number_timeout(search_wire: _SearchWire) -> None:
    """A non-numeric `timeout` fails at the SDK boundary before any HTTP request."""
    from meiva_builtins.plugins.meiva_memory.sdk import search_unwrapped as sdk_search

    with pytest.raises(TypeError, match="timeout must be"):
        sdk_search("q", timeout="fast")  # type: ignore[arg-type]
    assert search_wire.requests == []


def test_search_prefixes_paths_with_memory_path(search_wire: _SearchWire) -> None:
    """Gateway-relative paths become absolute paths in this machine's memory pool."""
    search_wire.results.extend(
        [
            MemorySearchResult("notes/foo.md", "desc1", (), 0.9, "foo passage"),
            MemorySearchResult("bar.md", "", (), 0.4, "bar passage"),
        ]
    )
    results = meiva.memory.search("any")
    assert [m.path for m in results] == [
        meiva.memory.SHARED_PATH / "notes" / "foo.md",
        meiva.memory.SHARED_PATH / "bar.md",
    ]
    assert all(m.path.is_absolute() for m in results)


def test_search_empty_result(search_wire: _SearchWire) -> None:
    """Gateway returns [] (no match) → SDK also returns []."""
    assert meiva.memory.search("nothing matches") == []


# ── IndexerUnavailable exception ─────────────────────────────────────────


def test_indexer_unavailable_is_exception() -> None:
    """IndexerUnavailable is an Exception subclass — agent can catch it."""
    assert issubclass(meiva.memory.IndexerUnavailable, Exception)


def test_indexer_unavailable_is_wire_encoded() -> None:
    """After PR-1, IndexerUnavailable travels over the wire protocol (MeivaAgentError
    subclass) — gateway side 503 + reason='indexer_unavailable', SDK reverse-looksup
    to reconstruct."""
    from base.api_contracts.agent_contract import MeivaAgentError

    assert issubclass(meiva.memory.IndexerUnavailable, MeivaAgentError)


def test_indexer_unavailable_importable_but_not_in_all() -> None:
    """exception class is not in __all_for_meiva__ (the rendered SDK surface only exposes
    the call surface), but remains reachable — agent still catches with
    `meiva.memory.IndexerUnavailable`."""
    from base.api_contracts.agent_contract import IndexerUnavailable

    assert "IndexerUnavailable" not in meiva.memory.__all_for_meiva__
    assert meiva.memory.IndexerUnavailable is IndexerUnavailable


def test_path_exported_in_all() -> None:
    assert "SHARED_PATH" in meiva.memory.__all_for_meiva__


def test_search_exported_in_all() -> None:
    assert "search" in meiva.memory.__all_for_meiva__


def test_search_detailed_removed() -> None:
    assert "search_detailed" not in meiva.memory.__all_for_meiva__
    assert not hasattr(meiva.memory, "search_detailed")


# -- search returns MemoryMatch tuples --


def test_search_returns_named_matches(search_wire: _SearchWire) -> None:
    """Each match carries the absolute path, description, tags, score and chunk, in order."""
    search_wire.results.extend(
        [
            MemorySearchResult(
                "notes/project.md", "Project note", ("type/project", "x"), 0.82, "the passage"
            ),
            MemorySearchResult("bar.md", "", (), 0.3, "other"),
        ]
    )
    results = meiva.memory.search("project")
    assert results == [
        meiva.memory.MemoryMatch(
            meiva.memory.SHARED_PATH / "notes" / "project.md",
            "Project note",
            ["type/project", "x"],
            0.82,
            "the passage",
        ),
        meiva.memory.MemoryMatch(meiva.memory.SHARED_PATH / "bar.md", "", [], 0.3, "other"),
    ]
    first = results[0]
    assert (first.path, first.description, first.tags, first.score, first.chunk) == tuple(first)
    assert isinstance(first.tags, list)


def test_memory_match_is_exported_for_help() -> None:
    """The result type is on the agent surface so `help(meiva.memory)` renders its fields."""
    assert "MemoryMatch" in meiva.memory.__all_for_meiva__
    assert meiva.memory.MemoryMatch._fields == ("path", "description", "tags", "score", "chunk")


def test_search_empty_result_desc(search_wire: _SearchWire) -> None:
    """No matches returns empty list."""
    assert meiva.memory.search("nothing") == []


def test_search_default_k_desc(search_wire: _SearchWire) -> None:
    """Default k=5."""
    meiva.memory.search("q")
    [request] = search_wire.requests
    assert json.loads(request.content)["k"] == 5


# meiva:note
# Runs child_face_boundary.py in a clean subprocess via trusted sys.executable to verify that
# importing the plugin's subprocess face pulls in no decision stack, agent runtime, or new httpx.
def test_the_subprocess_face_imports_no_decision_stack_agent_runtime_or_new_http_client() -> None:
    import subprocess
    import sys

    from . import child_face_boundary

    child = subprocess.run(  # noqa: S603
        [sys.executable, child_face_boundary.__file__],
        check=True,
        capture_output=True,
        text=True,
    )
    boundary = json.loads(child.stdout)
    assert boundary["imported"][1:] == ["meiva_builtins.plugins.meiva_memory.plugin"]
    assert boundary["absolute"] == []
    assert boundary["added_httpx"] == []


def test_an_external_harness_is_told_to_prefer_meiva_memory_over_its_own() -> None:
    from base.api_contracts.agent_contract import Harness
    from meiva_builtins.plugins.meiva_memory import agent_runtime

    (contribution,) = agent_runtime.contribute().harness_contributions
    assert Harness.CLAUDE_CODE in contribution.harnesses
    (rule,) = contribution.overlaps
    assert "in preference to your CLI's own memory" in rule
    assert "shared memory" in rule and "personal memory" in rule
    assert "shared with the cluster" not in rule


def test_an_external_harness_is_told_where_both_memories_live(tmp_path: Path) -> None:
    from base.api_contracts.agent_contract import Harness
    from base.lm.harness.handoff import Handoff
    from meiva_builtins.plugins.meiva_memory import agent_runtime

    home = tmp_path / "home"
    workspace = workspace_dir(7, home=home)
    handoff = Handoff(
        agent_id=7, instruction="Work.", machine="m", workspace=workspace, home=home
    ).with_contributions(agent_runtime.contribute().harness_contributions, harness=Harness.CODEX)
    text = handoff.render()
    (section,) = handoff.sections
    shared, personal = memory_dir(home=home), workspace / "memory"
    assert section.title == "Meiva Memory"
    for path in (
        shared,
        shared / "MEMORY.md",
        personal,
        personal / "MEMORY.md",
        personal / "INHERIT.md",
    ):
        assert f"`{path}`" in text
    assert "meiva.help(meiva.memory)" in text
    assert "meiva.files" not in text
    boundaries = text.index("## Tool & Environment Boundaries")
    reporting = text.index("## Collaboration & Reporting")
    assert boundaries < text.index("## Meiva Memory") < reporting


_CHINESE_HARD_RULE = "## \u26a0\ufe0f \u8bed\u8a00\uff1a\u6240\u6709\u8f93\u51fa\u7528\u4e2d\u6587\uff08\u786c\u7ea6\u675f\uff09"


@pytest.mark.parametrize("harness", [Harness.CLAUDE_CODE, Harness.CODEX, Harness.ANTIGRAVITY])
def test_handoff_embeds_shared_personal_and_inherit_bodies_for_all_harnesses(
    tmp_path: Path, harness: Harness
) -> None:
    from base.lm.harness.wiring import handoff_for
    from meiva_builtins.plugins.meiva_memory import agent_runtime

    home = tmp_path / "home"
    workspace = workspace_dir(7, home=home)
    shared = memory_dir(home=home)
    shared.mkdir(parents=True)
    (shared / "MEMORY.md").write_text(
        f"# Memory \u2014 \u5171\u4eab\u7528\u6237\u7d22\u5f15\n{_CHINESE_HARD_RULE}\n- [Rule](r.md) \u2014 Keep concise\n",
        encoding="utf-8",
    )
    personal = workspace / "memory"
    personal.mkdir(parents=True)
    (personal / "MEMORY.md").write_text(
        "- [My Note](my-note.md) — Personal durable note\n", encoding="utf-8"
    )
    (personal / "INHERIT.md").write_text(
        "---\ndescription: inherited\n---\n\nAlways verify against real code.\n",
        encoding="utf-8",
    )

    doc = handoff_for(
        harness,
        agent_id=7,
        machine="m",
        workspace=workspace,
        home=home,
        contributions=agent_runtime.contribute().harness_contributions,
    )
    rendered = doc.render()

    assert _CHINESE_HARD_RULE in rendered
    assert "- [Rule](r.md) — Keep concise" in rendered
    assert "- [My Note](my-note.md) — Personal durable note" in rendered
    assert "Always verify against real code." in rendered
    assert "description: inherited" not in rendered
    assert f"### Shared Memory Index (`{shared}/MEMORY.md`)" in rendered
    assert f"### Personal Memory Index (`{personal}/MEMORY.md`)" in rendered
    assert f"### Inherited Memory (`{personal}/INHERIT.md`)" in rendered


def test_handoff_applies_empty_and_length_protection_contracts(tmp_path: Path) -> None:
    from base.api_contracts.agent_contract import Harness
    from base.lm.harness.handoff import Handoff
    from meiva_builtins.plugins.meiva_memory import agent_runtime

    home = tmp_path / "home"
    workspace = workspace_dir(8, home=home)
    shared = memory_dir(home=home)
    shared.mkdir(parents=True)
    (shared / "MEMORY.md").write_text("   \n", encoding="utf-8")

    empty_doc = Handoff(
        agent_id=8, instruction="Work.", machine="m", workspace=workspace, home=home
    ).with_contributions(
        agent_runtime.contribute().harness_contributions, harness=Harness.CLAUDE_CODE
    )
    empty_text = empty_doc.render()
    assert "### Shared Memory Index" not in empty_text
    assert "### Inherited Memory" not in empty_text
    assert "### Personal Memory Index" in empty_text
    assert "(no content)" in empty_text
    assert (workspace / "memory" / "MEMORY.md").is_file()

    personal = workspace / "memory"
    (personal / "MEMORY.md").write_text("item\n" * 201, encoding="utf-8")
    (personal / "INHERIT.md").write_text("x" * 4050, encoding="utf-8")

    capped_doc = Handoff(
        agent_id=8, instruction="Work.", machine="m", workspace=workspace, home=home
    ).with_contributions(agent_runtime.contribute().harness_contributions, harness=Harness.CODEX)
    capped_text = capped_doc.render()
    assert "This index is 201 lines, over the 200-line soft cap" in capped_text
    assert "[truncated at 4000 chars — full entry:" in capped_text


class _ResumedMemorySession:
    def __init__(self, session_id: str) -> None:
        self.session_id: str | None = None
        self._sid = session_id
        self.generation = 1
        self.resumed = True
        self.exit_code: int | None = None
        self.submitted: list[str] = []
        self._queue: list[object] = []
        self._closed = False
        self._n = 0

    async def start(self, *, resume: str | None = None) -> None:
        del resume
        self.session_id = self._sid

    async def deliver(self, inputs: Sequence[object]) -> None:
        from base.lm.harness import HarnessInput, InputInContext, TurnCompleted, Usage

        for item in inputs:
            assert isinstance(item, HarnessInput)
            self._n += 1
            tid = f"t{self._n}"
            self.submitted.append(item.text)
            self._queue.append(InputInContext(item.key, tid, 1))
            self._queue.append(
                TurnCompleted(
                    turn_id=tid,
                    session_id=self._sid,
                    text="ok",
                    subtype="success",
                    is_error=False,
                    interrupted=False,
                    stop_reason="end_turn",
                    num_turns=1,
                    duration_ms=1,
                    usage=Usage(input_tokens=1, output_tokens=1),
                    cost_usd=0.0,
                    cumulative_cost_usd=0.0,
                    permission_denials=(),
                )
            )

    async def reconcile(self, keys: Sequence[str]) -> Mapping[str, object]:
        from base.lm.harness import Reconciliation

        return dict.fromkeys(keys, Reconciliation.NOT_FOUND)

    def adopt(self, keys: Sequence[str]) -> None:
        del keys

    def events(self) -> AsyncIterator[object]:
        async def stream() -> AsyncIterator[object]:
            while not self._closed:
                if self._queue:
                    yield self._queue.pop(0)
                else:
                    await asyncio.sleep(0.001)

        return stream()

    async def interrupt(self, *, turn_id: str | None = None) -> object:
        del turn_id
        return object()

    async def close(self, *, reason: str = "closed") -> None:
        del reason
        self._closed = True


async def test_memory_plugin_bridge_resumed_session_self_heals_and_tracks_changes(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime
    from typing import cast

    from base.clock.tests.fakes import system_clock
    from base.clock.tests.seeded_rng import seeded_rng
    from base.lm.harness import (
        FileSessionDigestStore,
        FileSessionRegistry,
        HarnessBridge,
        HarnessInput,
        HarnessSession,
        SessionRecord,
        TurnCompleted,
    )
    from base.lm.harness.handoff import instructions_path, refreshes_for
    from base.lm.harness.registration import SecureRandom
    from base.lm.harness.wiring import handoff_for
    from base.paths import harness_sessions_dir
    from meiva_builtins.plugins.meiva_memory import agent_runtime

    home = tmp_path / "home"
    workspace = workspace_dir(11, home=home)
    shared = memory_dir(home=home)
    shared.mkdir(parents=True)
    (shared / "MEMORY.md").write_text(f"{_CHINESE_HARD_RULE}\n", encoding="utf-8")

    personal = workspace / "memory"
    personal.mkdir(parents=True)
    personal_index = personal / "MEMORY.md"
    personal_index.write_text("- [Initial](init.md) — initial personal\n", encoding="utf-8")

    sessions_dir = harness_sessions_dir(home=home)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    FileSessionRegistry(sessions_dir).save(
        SessionRecord(
            agent_id=11,
            session_id="legacy-session-11",
            cwd=str(workspace),
            model="claude-sonnet-5-5",
            created_at=now,
            updated_at=now,
            harness="claude_code",
            generation=1,
        )
    )

    session = _ResumedMemorySession("legacy-session-11")
    inst_file = instructions_path(workspace)
    doc = handoff_for(
        Harness.CLAUDE_CODE,
        agent_id=11,
        machine="m",
        workspace=workspace,
        home=home,
        contributions=agent_runtime.contribute().harness_contributions,
    ).with_reference_file(inst_file)

    def _make_session(_handle: str) -> HarnessSession:
        return cast(HarnessSession, session)

    bridge = HarnessBridge(
        Harness.CLAUDE_CODE,
        11,
        _make_session,
        machine="m",
        handoff=doc,
        home=home,
        workspace=workspace,
        refreshes=refreshes_for(
            agent_runtime.contribute().harness_contributions, harness=Harness.CLAUDE_CODE
        ),
        digest_store=FileSessionDigestStore(sessions_dir),
        registration_ttl_seconds=3600.0,
        clock=system_clock(),
        handle_rng=SecureRandom(seeded_rng(1)),
        controller_pid=1234,
        timestamp_prefix=lambda: "",
    )

    async def _run(text: str, key: str) -> None:
        sub = await bridge.subscribe()
        try:
            await bridge.deliver([HarnessInput.of(key=key, text=text)])
            while (ev := await sub.get()) is not None:
                if isinstance(ev, TurnCompleted):
                    return
        finally:
            sub.close()

    await _run("turn-1", "k1")
    await _run("turn-2", "k2")
    personal_index.write_text("- [Updated](u.md) — changed rule\n", encoding="utf-8")
    await _run("turn-3", "k3")
    await _run("turn-4", "k4")

    assert "# Role & Operating Context" not in session.submitted[0]
    assert "[Meiva notice] Memory Index refreshed" in session.submitted[0]
    assert f"`{inst_file}` has also been updated" in session.submitted[0]
    assert _CHINESE_HARD_RULE in session.submitted[0]
    assert "- [Initial](init.md) — initial personal" in session.submitted[0]
    assert session.submitted[1] == "turn-2"
    assert "[Meiva notice] Memory Index refreshed" in session.submitted[2]
    assert "- [Updated](u.md) — changed rule" in session.submitted[2]
    assert session.submitted[3] == "turn-4"
