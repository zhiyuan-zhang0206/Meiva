from __future__ import annotations

import pytest

from scripts.host_ops.onboarding import catalog, harness_onboarding, transport
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


class _FakeHost(Host):
    # meiva:note
    # A host whose shell answers by command, so an install plan is testable without ssh. An
    # answer is one (exit_code, stdout) pair, or a list consumed in order so a command can
    # answer differently after another one ran (a link making a binary visible).
    # meiva:link scripts/host_ops/docs/harness-onboarding.meiva.okf.md

    def __init__(self, answers: dict[str, tuple[int, str] | list[tuple[int, str]]]) -> None:
        super().__init__("fake")
        self.answers = answers
        self.asked: list[tuple[str, bool]] = []

    def run(
        self,
        command: str,
        *,
        stdin: bytes | None = None,
        timeout: float = 300.0,
        agent_path: bool = False,
    ) -> transport.Result:
        self.asked.append((command, agent_path))
        answer = self.answers.get(command, (0, ""))
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        code, out = answer
        return transport.Result(self.name, command, code, out, "")


def test_install_command_redirects_an_unwritable_npm_root() -> None:
    command = catalog.install_command(catalog.spec("dsh"), "0.1.5-rc.3", prefix='"$HOME/.local"')
    assert command == 'npm install -g --prefix "$HOME/.local" @deepseek-ai/dsh@0.1.5-rc.3'


def test_npm_global_root_is_writable_only_when_the_shell_can_write_it() -> None:
    writable = _FakeHost({"npm root -g": (0, "/opt/homebrew/lib/node_modules\n")})
    assert harness_onboarding.npm_global_root_writable(writable) is True

    root_owned = _FakeHost(
        {
            "npm root -g": (0, "/usr/lib/node_modules\n"),
            'test -w "/usr/lib/node_modules"': (1, ""),
        }
    )
    assert harness_onboarding.npm_global_root_writable(root_owned) is False


def test_npm_root_is_not_writable_when_npm_cannot_answer() -> None:
    silent = _FakeHost({"npm root -g": (1, "")})
    assert harness_onboarding.npm_global_root_writable(silent) is False


def test_a_user_local_install_is_linked_where_the_agent_looks() -> None:
    host = _FakeHost(
        {
            "command -v dsh": [(1, ""), (0, "/usr/local/bin/dsh\n")],
            "test -w /usr/local/bin": (0, ""),
            'ln -sf "$HOME/.local/bin/dsh" "/usr/local/bin/dsh"': (0, ""),
        }
    )
    note, problem = harness_onboarding.reach_agent_path(host, catalog.spec("dsh"))
    assert problem == ""
    assert "linked /usr/local/bin/dsh" in note
    assert ("command -v dsh", True) in host.asked


def test_an_unreachable_install_says_what_to_run() -> None:
    host = _FakeHost({"command -v dsh": (1, ""), "test -w /usr/local/bin": (1, "")})
    note, problem = harness_onboarding.reach_agent_path(host, catalog.spec("dsh"))
    assert note == ""
    assert "sudo ln -sf ~/.local/bin/dsh /usr/local/bin/dsh" in problem
    assert not any(command.startswith("ln ") for command, _ in host.asked)


def test_an_already_reachable_install_needs_no_link() -> None:
    host = _FakeHost({"command -v dsh": (0, "/home/someone/.local/bin/dsh\n")})
    note, problem = harness_onboarding.reach_agent_path(host, catalog.spec("dsh"))
    assert (note, problem) == ("", "")
    assert not any(command.startswith("ln ") for command, _ in host.asked)
