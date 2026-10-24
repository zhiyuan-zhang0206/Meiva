"""Meiva code plugin cases: read wrap corrupted state field raises."""

from __future__ import annotations

import inspect
import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

import meiva
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_contract import BuildContext
from base.lm.effort import ReasoningEffort
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import PluginContributions, SdkNamespace, SdkWrap
from meiva.sdk_surface import install, process_context
from meiva.tests.fixtures.gateway_wire import GatewayStub
from meiva.tests.fixtures.gateway_wire import gateway_stub as gateway_stub
from meiva.tests.fixtures.scripted_chat_model import Script
from meiva_builtins.plugins.meiva_code.tests.surface_support import (
    authority_with,
    install_code_surface,
)
from meiva_builtins.plugins.meiva_code.tests.test_meiva_code_plugin import (
    get_injected_context_notes,
    make_git_repo,
    make_state_with_cwd,
)
from meiva_builtins.plugins.meiva_code.tests.test_meiva_code_plugin import (
    load_meiva_code_plugin as load_meiva_code_plugin,
)
from meiva_builtins.plugins.meiva_code.tests.test_meiva_code_plugin import (
    understand_script as understand_script,
)


def test_read_wrap_corrupted_state_field_raises(tmp_path: Path, default_home: Path):
    """sibling mutates meiva.state.meiva_code__injected_paths to non-valid type → handle.read()
    Pydantic validation raises (handle.read goes through cls.model_validate, schema type error explodes on the spot)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    make_git_repo(repo)
    (repo / "AGENTS.md").write_text("X")
    (repo / "foo.py").write_text("")

    state = make_state_with_cwd(str(repo))
    object.__setattr__(state, "meiva_code__injected_paths", 42)
    process_context.set_state(state)
    process_context.set_state_update({})
    try:
        with pytest.raises(Exception, match=r"injected_paths"):
            meiva.files.read("foo.py")
    finally:
        process_context.unbind_exec_turn()


def test_set_cwd_relative_resolves_against_current_cwd(tmp_path: Path):
    """set_cwd accepts relative path → resolve against current get_cwd()."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sub = repo / "src"
    sub.mkdir()

    process_context.set_state(make_state_with_cwd(str(repo)))
    process_context.set_state_update({})
    try:
        meiva.cwd.set("src")  # relative path
        assert meiva.state_update["meiva_code__cwd"] == str(sub.resolve())
    finally:
        process_context.unbind_exec_turn()


def test_set_cwd_expanduser_supported(tmp_path: Path, default_home: Path):
    """set_cwd accepts `~/...` uses expanduser, resolves via $HOME."""
    sub = default_home / "project"
    sub.mkdir()

    process_context.set_state(make_state_with_cwd(str(tmp_path)))
    process_context.set_state_update({})
    try:
        meiva.cwd.set("~/project")
        assert meiva.state_update["meiva_code__cwd"] == str(sub.resolve())
    finally:
        process_context.unbind_exec_turn()


def test_uninstall_restores_original(tmp_path: Path):
    """uninstall restores every wrapped target to its captured original and removes the
    plugin's namespace.

    The chained callable presents as the function it replaced — same `__name__` /
    `__module__` as the original — so "is it wrapped" is a registry question
    (`meiva.extend.stack`), not a `__module__` sniff. After uninstall the registry is empty and
    the namespace holds a different object (the original).
    """
    from meiva.sdk_surface import wraps

    # fixture already installed meiva_code -> files.read carries one wrap layer
    assert wraps.stack("files.read")  # non-empty: wrapped
    wrapped = meiva.files.read

    install.uninstall()
    assert wraps.stack("files.read") == []  # registry emptied
    assert meiva.files.read is not wrapped  # restored to the original object
    assert meiva.files.read.__module__ == "meiva.files"
    assert not hasattr(meiva, "cwd")


def test_duplicate_namespace_refuses_the_second_plugin_whole(
    model_catalog: ModelCatalog, config_authority: ConfigAuthority
):
    """A second plugin declaring `cwd` is refused and rolled back whole: its earlier namespace and
    its wrap are not installed, and meiva_code's own surface is untouched."""
    from meiva_builtins.plugins.meiva_code import _code_namespace

    def _other_wrap(inner: Callable[..., object], path: str, *args: object, **kwargs: object):
        return inner(path, *args, **kwargs)

    install.uninstall()
    other = PluginContributions(
        sdk_namespaces=(
            SdkNamespace("other_ns", _code_namespace),
            SdkNamespace("cwd", _code_namespace),
        ),
        sdk_wraps=(SdkWrap("files.read", _other_wrap),),
    )
    admitted = install_code_surface(model_catalog, config_authority, ("other_plugin", other))

    assert [name for name, _ in admitted.plugins] == ["meiva_code"]
    assert not hasattr(meiva, "other_ns")
    assert [p for p, _ in meiva.extend.stack("files.read")] == ["meiva_code"]


def test_second_wrap_stacks_instead_of_asserting(
    model_catalog: ModelCatalog, config_authority: ConfigAuthority
):
    """A second wrapper on meiva.files.read stacks on top of meiva_code's, in
    deterministic registration order — the retired exclusivity assert used to
    reject this. `meiva.extend.stack` shows both layers; the dedup that used to
    justify the assert now holds because meiva_code calls `inner` exactly once
    regardless of how many layers sit below."""
    # fixture already installed meiva_code -> layer 1 on files.read
    before = meiva.extend.stack("files.read")
    assert [p for p, _ in before] == ["meiva_code"]

    def _external_wrap(inner: Callable[..., object], path: str, *args: object, **kwargs: object):
        return inner(path, *args, **kwargs)

    install.uninstall()
    install_code_surface(
        model_catalog,
        config_authority,
        ("other_plugin", PluginContributions(sdk_wraps=(SdkWrap("files.read", _external_wrap),))),
    )

    after = meiva.extend.stack("files.read")
    assert [p for p, _ in after] == ["meiva_code", "other_plugin"]  # stacked, no assert


def test_plugin_wraps_all_files_ops_for_cwd():
    """plugin meiva_code wraps all meiva.files.* operations (+ shell.run / understand) —
    not just read.

    read additionally handles AGENTS.md auto-injection; write / append / edit / delete / glob
    only do cwd path resolution (no AGENTS.md injection), ensuring that after agent meiva.cwd.set(), all
    file operation paths are consistent. Use introspection via `meiva.extend.stack` to verify each target
    is wrapped by meiva_code — the chained wrapper masquerades __module__ as the original, so a
    `__module__ != "meiva.files"` probe fails; wrap fact lives in registry.
    """
    # fixture already installed meiva_code plugin
    for target in (
        "files.read",
        "files.write",
        "files.append",
        "files.edit",
        "files.glob",
        "files.delete",
        "shell.run",
        "understand",
    ):
        stack = meiva.extend.stack(target)
        assert [p for p, _ in stack] == ["meiva_code"], target


def echo_run(cmd: str, *, cwd: str | None, timeout: float) -> str:
    return json.dumps([cmd, cwd, timeout])


def test_shell_run_wrap_passes_logical_cwd_without_changing_process_cwd(tmp_path: Path):
    """The shell wrapper passes logical state as an explicit subprocess cwd;
    it never relies on or mutates the Python process cwd."""
    from meiva_builtins.plugins.meiva_code.plugin import wrapped_shell_run

    logical_cwd = tmp_path / "logical"
    logical_cwd.mkdir()
    process_cwd = Path.cwd()

    process_context.set_state(make_state_with_cwd(str(logical_cwd)))
    process_context.set_state_update({})
    try:
        assert json.loads(wrapped_shell_run(echo_run, "pwd", timeout=7.0)) == [
            "pwd",
            str(logical_cwd),
            7.0,
        ]
        assert Path.cwd() == process_cwd
    finally:
        process_context.unbind_exec_turn()


# meiva:note
# The wrapped `meiva.shell.run` starts the command in the tracked directory.
def test_shell_run_wrap_runs_in_the_logical_cwd(tmp_path: Path):
    logical_cwd = tmp_path / "logical"
    logical_cwd.mkdir()

    process_context.set_state(make_state_with_cwd(str(logical_cwd)))
    process_context.set_state_update({})
    try:
        assert Path(str(meiva.shell.run("pwd", timeout=30.0)).strip()).resolve() == (
            logical_cwd.resolve()
        )
        other = tmp_path / "explicit"
        other.mkdir()
        assert Path(
            str(meiva.shell.run("pwd", cwd=str(other), timeout=30.0)).strip()
        ).resolve() == (other.resolve())
    finally:
        process_context.unbind_exec_turn()


def echo_serve(
    dir: str,
    name: str,
    port: int | None = None,
    title: str | None = None,
    *,
    ttl: float | None = None,
) -> tuple[str, str, int | None, str | None, float | None]:
    return dir, name, port, title, ttl


def test_serve_wrap_resolves_relative_dir_against_cwd(tmp_path: Path):
    from meiva_builtins.plugins.meiva_code.plugin import wrapped_serve

    process_context.set_state(make_state_with_cwd(str(tmp_path)))
    process_context.set_state_update({})
    try:
        assert wrapped_serve(echo_serve, "site", "preview", port=8123, title="Preview") == (
            str((tmp_path / "site").resolve()),
            "preview",
            8123,
            "Preview",
            None,
        )
    finally:
        process_context.unbind_exec_turn()


def test_serve_wrap_passes_absolute_dir_through(tmp_path: Path):
    from meiva_builtins.plugins.meiva_code.plugin import wrapped_serve

    logical_cwd = tmp_path / "cwd"
    logical_cwd.mkdir()
    absolute_dir = tmp_path / "site"
    process_context.set_state(make_state_with_cwd(str(logical_cwd)))
    process_context.set_state_update({})
    try:
        served = wrapped_serve(echo_serve, str(absolute_dir), "preview")
        assert served[0] == str(absolute_dir.resolve())
    finally:
        process_context.unbind_exec_turn()


def test_serve_wrap_registered():
    assert meiva.extend.stack("ui.serve")


def test_set_cwd_surfaces_and_stores_cwd_note(tmp_path: Path):
    """set_cwd into a repo with `.agents/skills/` surfaces those skills under
    `meiva.skills.*` (via the provider the plugin registered) and stores a
    cwd_note for the after-exec hook to inject as a system reminder."""
    import meiva.skills as meiva_skills

    repo = tmp_path / "repo"
    demo = repo / ".meiva" / "skills" / "demo-proj"
    demo.mkdir(parents=True)
    (demo / "SKILL.md").write_text(
        "---\nname: demo-proj\ndescription: a project-local demo\n---\nbody",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    process_context.set_state(make_state_with_cwd(str(tmp_path)))
    process_context.set_state_update({})
    try:
        meiva.cwd.set(repo)
        # No print output — cwd_note is set in state for the after-exec hook
        assert meiva.state.meiva_code__cwd_note is not None  # type: ignore[union-attr]
        assert f"Working directory set to {repo}" in meiva.state.meiva_code__cwd_note  # type: ignore[union-attr]
        assert "demo-proj" in {s["name"] for s in meiva_skills.names()}
    finally:
        process_context.unbind_exec_turn()


def test_set_cwd_non_git_stores_cwd_note(tmp_path: Path):
    """A cwd not under a git repo sets cwd_note with just the path
    (no project-skills listing)."""
    process_context.set_state(make_state_with_cwd(str(tmp_path)))
    process_context.set_state_update({})
    try:
        meiva.cwd.set(tmp_path)
        # No print output — cwd_note is set in state
        note = meiva.state.meiva_code__cwd_note  # type: ignore[union-attr]
        assert note is not None
        assert f"Working directory set to {tmp_path}" == note
    finally:
        process_context.unbind_exec_turn()


def test_coding_tools_section_skips_framework_expanded_modules():
    """A module in the effective expand view (settings + plugin registrations)
    is already rendered (full contract) by the framework section — the plugin
    must not promote it a second time. Exact path match: an expand entry for a
    child (e.g. `shell.sessions`) does not suppress the parent's stub. `cwd`
    is declared as an expanded namespace by `contribute()`, so it is
    always expanded and never promoted here."""
    from meiva_builtins.plugins.meiva_code._prompt_sections import coding_tools_section

    text = coding_tools_section(
        AgentSlices.resolve(
            default_reader=defaults(agent__sdk_expand_in_system_prompt=["files", "shell.sessions"]),
        ),
        catalog=build_model_catalog(),
    )
    assert "## meiva.files" not in text  # expanded by the framework -> skipped
    assert "## meiva.shell" in text  # only the child is expanded -> parent stays
    assert "## meiva.cwd" not in text  # plugin-registered expand -> skipped too
    assert "Use the Meiva file and shell tools" in text  # preamble always renders


def test_coding_tools_section_all_expanded_keeps_preamble_only():
    from meiva_builtins.plugins.meiva_code._prompt_sections import coding_tools_section

    text = coding_tools_section(
        AgentSlices.resolve(
            default_reader=defaults(agent__sdk_expand_in_system_prompt=["cwd", "files", "shell"]),
        ),
        catalog=build_model_catalog(),
    )
    assert text.startswith("# Coding tools")
    assert "Use the Meiva file and shell tools" in text
    # search steering: rg over recursive grep/find (recursive grep times out on
    # the worktree-heavy checkout) — guard for the user-reported slowness
    assert "`rg` (ripgrep)" in text
    assert "## meiva." not in text
    assert not text.endswith("\n\n")


def echo_understand(
    targets: list[dict[str, str | list[str]]], *, effort: str, max_concurrent: int | None
) -> list[str]:
    return [json.dumps({"targets": targets, "effort": effort, "max_concurrent": max_concurrent})]


def forwarded(inner_result: list[str]) -> dict[str, Any]:
    [encoded] = inner_result
    return json.loads(encoded)


def test_understand_wrap_resolves_paths_against_cwd(tmp_path: Path, understand_script: Script):
    """In-turn relative paths= resolved against tracked cwd — same string same file as files.read."""
    (tmp_path / "rel.txt").write_text("cwd material", encoding="utf-8")
    process_context.set_state(make_state_with_cwd(str(tmp_path)))
    process_context.set_state_update({})
    try:
        [out] = meiva.understand([{"prompt": "p", "paths": ["rel.txt"]}])
        assert out == "ok"
        [message] = understand_script.requests[-1]
        assert message.content[0] == {"type": "text", "text": "cwd material"}
    finally:
        process_context.unbind_exec_turn()


def test_understand_wrap_missing_paths_raises_with_cwd_location(tmp_path: Path):
    """In-turn relative paths= that does not exist under cwd → FileNotFoundError points to cwd-resolved result
    (same semantics as files.read, won't silently treat as text)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    process_context.set_state(make_state_with_cwd(str(repo)))
    process_context.set_state_update({})
    try:
        with pytest.raises(FileNotFoundError, match=r"nope\.txt"):
            meiva.understand([{"prompt": "p", "paths": ["nope.txt"]}])
    finally:
        process_context.unbind_exec_turn()


def test_understand_wrap_outside_turn_defers_to_workspace(
    workspace: Path, understand_script: Script
):
    """Outside turn (not in_exec_turn) → workspace baseline resolution (via _resolve_for_cwd passthrough)."""
    assert not process_context.in_exec_turn()
    workspace.mkdir(parents=True)
    (workspace / "f.txt").write_text("ws material", encoding="utf-8")
    meiva.understand([{"prompt": "p", "paths": ["f.txt"]}])
    [message] = understand_script.requests[-1]
    assert message.content[0] == {"type": "text", "text": "ws material"}


def test_understand_wrap_keeps_error_attribute_and_doc():
    """After wrap, meiva.understand.UnderstandError still reachable (agent's documented catch path),
    docstring original contract preserved."""
    from meiva.understand import UnderstandError, understand  # the module's own names

    assert meiva.understand.UnderstandError is UnderstandError  # type: ignore[attr-defined] # pyright: ignore[reportFunctionMemberAccess]
    assert meiva.understand.__doc__ == understand.__doc__


def test_understand_wrap_passes_invalid_combo_to_core(tmp_path: Path):
    """A malformed target reaches the core untouched so its canonical ValueError
    fires — the wrap does not preempt validation, in either direction (a target
    carrying both `paths` and `text`, or neither)."""
    process_context.set_state(make_state_with_cwd(str(tmp_path)))
    process_context.set_state_update({})
    try:
        with pytest.raises(ValueError, match="exactly one"):
            meiva.understand([{"prompt": "p"}])
        with pytest.raises(ValueError, match="exactly one"):
            meiva.understand([{"prompt": "p", "paths": ["rel.txt"], "text": "t"}])
        # A `paths` value that is not a list of path strings also reaches the
        # core untouched, so its canonical TypeError fires (not a wrap error).
        with pytest.raises(TypeError, match="must be a list of file paths"):
            meiva.understand([{"prompt": "p", "paths": "rel.txt"}])
    finally:
        process_context.unbind_exec_turn()


# meiva:note
# This binding supplies a real provider client with a loopback endpoint; the installed SDK wrap,
# core plan, provider factory and OpenAI serializer all run before the listener sees the effort.
def _wire_model(ctx: BuildContext, *, endpoint: str, client: httpx.Client) -> BaseChatModel:
    return ChatOpenAI(
        model=ctx.model,
        api_key=SecretStr(ctx.keys.require("OPENAI_API_KEY")),
        base_url=endpoint,
        http_client=client,
        use_responses_api=True,
        reasoning={"effort": ctx.resolved_effort},
        disable_streaming=True,
        timeout=ctx.timeout,
    )


def test_understand_wrap_forwards_effort(
    gateway_stub: GatewayStub, model_catalog: ModelCatalog, config_authority: ConfigAuthority
):
    gateway_stub.reply(
        "POST",
        "/v1/responses",
        {
            "id": "resp_effort",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "gpt-6-sol",
            "service_tier": "default",
            "error": None,
            "output": [
                {
                    "id": "msg_1",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "ok", "annotations": list[object]()}
                    ],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        },
    )
    with httpx.Client(trust_env=False) as client:
        binding = replace(
            model_catalog.bindings["gpt-"],
            build=partial(_wire_model, endpoint=gateway_stub.url + "/v1", client=client),
        )
        catalog = replace(model_catalog, bindings={**model_catalog.bindings, "gpt-": binding})
        authority = authority_with(
            config_authority, understand_text_model="gpt-6-sol", llm_override=""
        )
        install.uninstall()
        install_code_surface(catalog, authority)
        targets: list[dict[str, str | list[str]]] = [{"prompt": "p", "text": "t"}]
        assert meiva.understand(targets) == ["ok"]
        assert meiva.understand(targets, effort="low") == ["ok"]
    assert [request.body["reasoning"]["effort"] for request in gateway_stub.received] == [
        "max",
        "low",
    ]
    assert inspect.signature(meiva.understand).parameters["effort"].default == ReasoningEffort.MAX


# meiva:note
# Every target runs through file loading and the core, retaining its material/prompt grouping.
def test_understand_wrap_resolves_every_target_in_a_batch(
    tmp_path: Path, understand_script: Script
):
    (tmp_path / "a.txt").write_text("material A", encoding="utf-8")
    (tmp_path / "b.txt").write_text("material B", encoding="utf-8")
    process_context.set_state(make_state_with_cwd(str(tmp_path)))
    process_context.set_state_update({})
    try:
        assert meiva.understand(
            [
                {"prompt": "p1", "paths": ["a.txt"]},
                {"prompt": "p2", "text": "inline"},
                {"prompt": "p3", "paths": ["b.txt"]},
            ]
        ) == ["ok", "ok", "ok"]
    finally:
        process_context.unbind_exec_turn()
    materials = [message.content for [message] in understand_script.requests]
    for body, prompt in (("material A", "p1"), ("inline", "p2"), ("material B", "p3")):
        assert any(
            content[:2] == [{"type": "text", "text": body}, {"type": "text", "text": prompt}]
            for content in materials
        )
    assert len(materials) == 3


def test_understand_wrap_resolves_every_entry_in_a_paths_list(
    tmp_path: Path, understand_script: Script
):
    """Each entry of a multi-file `paths` target is resolved against the tracked
    cwd — the wrap walks the list, not just the first file."""
    (tmp_path / "a.txt").write_text("material A", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("material B", encoding="utf-8")
    process_context.set_state(make_state_with_cwd(str(tmp_path)))
    process_context.set_state_update({})
    try:
        [out] = meiva.understand([{"prompt": "p", "paths": ["a.txt", "sub/b.txt"]}])
        assert out == "ok"
        [message] = understand_script.requests[-1]
        assert message.content[:3] == [
            {"type": "text", "text": "material A"},
            {"type": "text", "text": "material B"},
            {"type": "text", "text": "p"},
        ]
    finally:
        process_context.unbind_exec_turn()


def test_understand_wrap_forwards_max_concurrent(tmp_path: Path, understand_script: Script):
    """`max_concurrent` passes through the wrap to the core — a call with the
    knob completes, and the core's own validation still fires on a bad value
    (regression: the wrap used to drop the keyword and raise TypeError)."""
    from meiva_builtins.plugins.meiva_code.plugin import wrapped_understand

    targets: list[dict[str, str | list[str]]] = [{"prompt": "p", "text": "t"}]
    assert forwarded(wrapped_understand(echo_understand, targets, max_concurrent=2)) == {
        "targets": targets,
        "effort": "max",
        "max_concurrent": 2,
    }
    (tmp_path / "rel.txt").write_text("cwd material", encoding="utf-8")
    process_context.set_state(make_state_with_cwd(str(tmp_path)))
    process_context.set_state_update({})
    try:
        [out] = meiva.understand([{"prompt": "p", "paths": ["rel.txt"]}], max_concurrent=2)
        assert out == "ok"
        with pytest.raises(ValueError, match="at least 1"):
            meiva.understand([{"prompt": "p", "paths": ["rel.txt"]}], max_concurrent=0)
    finally:
        process_context.unbind_exec_turn()


def test_read_wrap_dedup_by_content_hash_across_paths(tmp_path: Path, default_home: Path):
    """Two AGENTS.md at different paths with identical content → only the first
    is auto-injected; the second is skipped by content-hash dedup. This is the
    worktree case: the worktree copy and the main repo copy share the same
    content but live at different absolute paths."""
    repo = tmp_path / "repo"
    repo.mkdir()
    make_git_repo(repo)
    # Main repo AGENTS.md
    agents_main = repo / "AGENTS.md"
    agents_main.write_text("SHARED CONTENT")
    # Worktree AGENTS.md (different path, same content)
    wt = repo / ".worktrees" / "wt1"
    wt.mkdir(parents=True)
    agents_wt = wt / "AGENTS.md"
    agents_wt.write_text("SHARED CONTENT")
    # A file deeper in the worktree — triggers walk across both AGENTS.md
    (wt / "src").mkdir()
    (wt / "src" / "foo.py").write_text("# code")

    process_context.set_state(make_state_with_cwd(str(wt / "src")))
    process_context.set_state_update({})
    try:
        meiva.files.read("foo.py")

        notes = get_injected_context_notes(meiva.state_update)
        # Only ONE injection — the second AGENTS.md has the same content hash
        assert len(notes) == 1
        assert "SHARED CONTENT" in notes[0]["content"]
        # Both paths are marked in injected_paths (the second skipped but still
        # marked so path-based check short-circuits next time)
        injected = meiva.state_update["meiva_code__injected_paths"]
        assert str(agents_wt.resolve()) in injected
        assert str(agents_main.resolve()) in injected
        # The hash set records the single hash
        assert "meiva_code__injected_hashes" in meiva.state_update
        assert len(meiva.state_update["meiva_code__injected_hashes"]) == 1
    finally:
        process_context.unbind_exec_turn()


def test_set_cwd_with_skills_stores_project_skills_note(tmp_path: Path):
    """set_cwd into a repo with project skills stores a summary string in
    project_skills_note for the after-exec hook to inject as a system note."""
    repo = tmp_path / "repo"
    demo = repo / ".meiva" / "skills" / "demo-proj"
    demo.mkdir(parents=True)
    (demo / "SKILL.md").write_text(
        "---\nname: demo-proj\ndescription: a project-local demo\n---\nbody",
        encoding="utf-8",
    )
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    process_context.set_state(make_state_with_cwd(str(tmp_path)))
    process_context.set_state_update({})
    try:
        meiva.cwd.set(repo)
        note = meiva.state.meiva_code__project_skills_note  # type: ignore[union-attr]
        assert note is not None
        assert "Skills available in this repo" in note
        assert "demo-proj" in note
        assert "a project-local demo" in note
    finally:
        process_context.unbind_exec_turn()


def test_read_wrap_hash_dedup_respects_different_content(tmp_path: Path, default_home: Path):
    """Two AGENTS.md at different paths with different content → both are
    injected. Hash dedup must not collapse distinct files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    make_git_repo(repo)
    agents_main = repo / "AGENTS.md"
    agents_main.write_text("MAIN CONTENT")
    wt = repo / ".worktrees" / "wt1"
    wt.mkdir(parents=True)
    agents_wt = wt / "AGENTS.md"
    agents_wt.write_text("WORKTREE CONTENT")
    (wt / "src").mkdir()
    (wt / "src" / "foo.py").write_text("# code")

    process_context.set_state(make_state_with_cwd(str(wt / "src")))
    process_context.set_state_update({})
    try:
        meiva.files.read("foo.py")

        notes = get_injected_context_notes(meiva.state_update)
        # Both injected — content differs
        assert len(notes) == 2
        contents = {e["content"] for e in notes}
        assert "MAIN CONTENT" in contents
        assert "WORKTREE CONTENT" in contents
        assert len(meiva.state_update["meiva_code__injected_hashes"]) == 2
    finally:
        process_context.unbind_exec_turn()
