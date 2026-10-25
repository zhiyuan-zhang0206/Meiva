from __future__ import annotations

from pathlib import Path

import pytest

from scripts.host_ops.onboarding import catalog, harness_onboarding
from scripts.host_ops.onboarding.harness_onboarding import (
    Evidence,
    format_cell,
    parse_pins,
    parse_version,
    redact,
)
from scripts.host_ops.onboarding.transport import Host, bind_request, parse_hosts


def test_install_command_pins_an_npm_package() -> None:
    command = catalog.install_command(catalog.spec("codex"), "0.163.0")
    assert command == "npm install -g @openai/codex@0.163.0"


def test_install_command_passes_the_version_to_a_script_installer() -> None:
    command = catalog.install_command(catalog.spec("claude"), "2.1.290")
    assert command == "curl -fsSL https://claude.ai/install.sh | bash -s -- 2.1.290"


def test_install_command_omits_the_version_when_the_vendor_cannot_pin() -> None:
    command = catalog.install_command(catalog.spec("agy"), "1.4.2")
    assert command == "curl -fsSL https://antigravity.google/cli/install.sh | bash"


def test_install_command_defaults_to_the_pinned_version() -> None:
    harness = catalog.spec("claude")
    assert harness.pinned_version in catalog.install_command(harness)


def test_spec_refuses_an_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown harness"):
        catalog.spec("cursor")


def test_codex_api_key_store_counts_as_static() -> None:
    harness = catalog.spec("codex")
    payload = b'{"auth_mode": "apikey", "OPENAI_API_KEY": "sk-proj-x"}'
    assert catalog.credential_risk(harness, harness.stores[0].path, payload) == "static"


def test_codex_chatgpt_store_keeps_the_catalog_verdict() -> None:
    harness = catalog.spec("codex")
    payload = b'{"auth_mode": "chatgpt", "tokens": {"refresh_token": "r"}}'
    assert (
        catalog.credential_risk(harness, harness.stores[0].path, payload)
        == harness.stores[0].rotation
    )


def test_an_unparseable_store_keeps_the_catalog_verdict() -> None:
    harness = catalog.spec("claude")
    assert (
        catalog.credential_risk(harness, harness.stores[0].path, b"not json")
        == harness.stores[0].rotation
    )


def test_deepseek_api_key_is_the_only_store_declared_static() -> None:
    static = [
        (harness, store.path)
        for harness in catalog.CATALOG.values()
        for store in harness.stores
        if store.rotation == "static"
    ]
    assert static == [(catalog.spec("dsh"), "${DSH_HOME:-~/.dsh}/.credentials.yaml")]


def test_claude_login_is_declared_single_use() -> None:
    harness = catalog.spec("claude")
    assert [store.rotation for store in harness.stores] == ["rotating"]


def test_the_ide_token_is_retargeted_to_the_cli_path() -> None:
    harness = catalog.spec("agy")
    legacy = [store for store in harness.stores if "jetski" in store.path]
    assert len(legacy) == 1
    assert legacy[0].destination == "~/.gemini/antigravity-cli/antigravity-oauth-token"


def test_parse_version_reads_the_first_dotted_version() -> None:
    assert parse_version("2.1.290 (Claude Code)") == "2.1.290"
    assert parse_version("codex-cli 0.163.0") == "0.163.0"
    assert parse_version("0.1.5-rc.3") == "0.1.5-rc.3"
    assert parse_version("no version here") == ""


def test_redact_removes_api_keys_from_surfaces() -> None:
    assert "sk-proj-abcdefghij" not in redact("Logged in using an API key - sk-proj-abcdefghij")


def test_bind_request_lands_before_the_entry_point() -> None:
    source = "from __future__ import annotations\nimport sys\n\nif __name__ == '__main__':\n    sys.exit(0)\n"
    patched = bind_request(source, {"op": "stat"})
    assert patched.index("__REQUEST__") < patched.index("if __name__")
    assert patched.startswith("from __future__")


def test_bind_request_appends_when_there_is_no_entry_point() -> None:
    assert bind_request("print(1)\n", {"op": "stat"}).endswith("\n")


def test_cell_shortens_overflow_with_an_ellipsis() -> None:
    assert format_cell("a" * 10, 4) == "aaa\u2026 "


def test_parse_pins_rejects_a_bare_word() -> None:
    with pytest.raises(SystemExit):
        parse_pins(["claude"])


def test_parse_hosts_defaults_to_this_machine_and_keeps_order() -> None:
    assert [host.name for host in parse_hosts(None)] == ["local"]
    assert [host.name for host in parse_hosts(["b", "a", "b"])] == ["b", "a"]


def test_evidence_flags_a_probe_that_contradicts_the_cli() -> None:
    row = Evidence("m", "claude_code", "available", "", "/x/claude", True, "2.1.290", "out")
    assert "signed out" in row.disagreement


def test_evidence_is_quiet_when_the_sources_agree() -> None:
    row = Evidence("m", "codex", "available", "", "/x/codex", True, "0.163.0", "in")
    assert row.disagreement == ""


def test_host_rejects_an_empty_name() -> None:
    with pytest.raises(ValueError):
        Host("")


def _write_exec(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def test_install_command_redirects_an_unwritable_npm_root() -> None:
    command = catalog.install_command(catalog.spec("dsh"), "0.1.5-rc.3", prefix='"$HOME/.local"')
    assert command == 'npm install -g --prefix "$HOME/.local" @deepseek-ai/dsh@0.1.5-rc.3'


def test_npm_global_root_is_writable_only_when_the_shell_can_write_it(tmp_path: Path) -> None:
    writable_root = tmp_path / "writable"
    writable_root.mkdir()
    writable_bin = tmp_path / "writable-bin"
    _write_exec(writable_bin / "npm", f'printf "%s\\n" "{writable_root}"')
    writable_host = Host("local", path_preamble=f'export PATH="{writable_bin}:/usr/bin:/bin"')
    assert harness_onboarding.npm_global_root_writable(writable_host) is True

    root_owned = tmp_path / "readonly"
    root_owned.mkdir()
    root_owned.chmod(0o555)
    readonly_bin = tmp_path / "readonly-bin"
    _write_exec(readonly_bin / "npm", f'printf "%s\\n" "{root_owned}"')
    readonly_host = Host("local", path_preamble=f'export PATH="{readonly_bin}:/usr/bin:/bin"')
    try:
        assert harness_onboarding.npm_global_root_writable(readonly_host) is False
    finally:
        root_owned.chmod(0o755)


def test_npm_root_is_not_writable_when_npm_cannot_answer(tmp_path: Path) -> None:
    silent_bin = tmp_path / "silent-bin"
    _write_exec(silent_bin / "npm", "exit 1")
    silent = Host("local", path_preamble=f'export PATH="{silent_bin}:/usr/bin:/bin"')
    assert harness_onboarding.npm_global_root_writable(silent) is False


def test_a_user_local_install_is_linked_where_the_agent_looks(tmp_path: Path) -> None:
    local_bin = tmp_path / "local-bin"
    _write_exec(local_bin / "dsh", "exit 0")
    link_dir = tmp_path / "agent-bin"
    link_dir.mkdir()
    host = Host("local", agent_path=f"{link_dir}:/usr/bin:/bin")

    note, problem = harness_onboarding.reach_agent_path(
        host,
        catalog.spec("dsh"),
        link_dir=str(link_dir),
        local_bin=str(local_bin),
    )
    assert problem == ""
    assert f"linked {link_dir}/dsh" in note
    assert (link_dir / "dsh").is_symlink()


def test_an_unreachable_install_says_what_to_run(tmp_path: Path) -> None:
    local_bin = tmp_path / "local-bin"
    _write_exec(local_bin / "dsh", "exit 0")
    link_dir = tmp_path / "readonly-agent-bin"
    link_dir.mkdir()
    link_dir.chmod(0o555)
    host = Host("local", agent_path=f"{link_dir}:/usr/bin:/bin")
    try:
        note, problem = harness_onboarding.reach_agent_path(
            host,
            catalog.spec("dsh"),
            link_dir=str(link_dir),
            local_bin=str(local_bin),
        )
        assert note == ""
        assert f"sudo ln -sf ~/.local/bin/dsh {link_dir}/dsh" in problem
        assert not (link_dir / "dsh").exists()
    finally:
        link_dir.chmod(0o755)


def test_an_already_reachable_install_needs_no_link(tmp_path: Path) -> None:
    agent_bin = tmp_path / "agent-bin"
    _write_exec(agent_bin / "dsh", "exit 0")
    link_dir = tmp_path / "unused-link-dir"
    host = Host("local", agent_path=f"{agent_bin}:/usr/bin:/bin")

    note, problem = harness_onboarding.reach_agent_path(
        host,
        catalog.spec("dsh"),
        link_dir=str(link_dir),
        local_bin=str(tmp_path / "local-bin"),
    )
    assert (note, problem) == ("", "")
    assert not link_dir.exists()
