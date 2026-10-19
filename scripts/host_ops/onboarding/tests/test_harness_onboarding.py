from __future__ import annotations

import pytest

from scripts.host_ops.onboarding import catalog
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
