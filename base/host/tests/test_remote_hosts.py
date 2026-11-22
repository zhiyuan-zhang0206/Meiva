from __future__ import annotations

from pathlib import Path

import pytest

from base.host.remote_hosts import (
    DEFAULT_WIN_HELPER_PORT,
    RemoteHostConfigError,
    ResolvedTarget,
    TargetResolutionError,
    has_remote_hosts,
    is_ip_literal,
    load_remote_hosts,
    parse_remote_hosts_toml,
    read_bearer_token,
    resolve_target,
)


def test_parse_remote_hosts_toml_explicit_fields_and_redacted_repr() -> None:
    raw = """
[hosts.zzyasus]
os = "windows"
ssh_host = "zzyasus"
helper_port = 18792
local_port = 18792
token_file = "~/.meiva/secrets/zzyasus.token"
notes = "Win11 · RTX 2060"
capabilities = "CC4 / Headshot"
"""
    hosts = parse_remote_hosts_toml(raw)
    zzy = hosts["zzyasus"]
    assert (zzy.alias, zzy.os, zzy.ssh_host) == ("zzyasus", "windows", "zzyasus")
    assert (zzy.helper_port, zzy.local_port) == (18792, 18792)
    assert zzy.target_uri == "ssh://zzyasus"
    assert zzy.summary_notes == "Win11 · RTX 2060 | CC4 / Headshot"
    assert zzy.protocol == "win_bridge_mcp"
    assert "zzyasus.token" not in repr(zzy)


def test_parse_remote_hosts_toml_defaults() -> None:
    hosts = parse_remote_hosts_toml('[hosts.studio-mac]\nos = "macos"\n')
    mac = hosts["studio-mac"]
    assert mac.ssh_host == "studio-mac"
    assert mac.helper_port == DEFAULT_WIN_HELPER_PORT
    assert mac.local_port is None
    assert mac.protocol == "computer_http"


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("not = [valid toml", "invalid remote_hosts.toml"),
        ("[other]\na = 1\n", "unknown top-level"),
        ('[hosts."10.20.30.40"]\nos = "windows"\n', "bare IP"),
        ('[hosts.bad]\nos = "solaris"\n', "'os' must be one of"),
        ('[hosts.bad]\nos = "windows"\nssh_host = "-oProxyCommand=id"\n', "safe SSH config alias"),
        ('[hosts.bad]\nos = "windows"\nhelper_port = 99999\n', "1..65535"),
        ('[hosts.bad]\nos = "windows"\nunknown_key = true\n', "unknown key"),
    ],
)
def test_parse_remote_hosts_toml_rejects_invalid_entries(raw: str, match: str) -> None:
    with pytest.raises(RemoteHostConfigError, match=match):
        parse_remote_hosts_toml(raw)


def test_load_remote_hosts_and_bearer_token_io(tmp_path: Path) -> None:
    home = tmp_path / "meiva_home"
    user_home = tmp_path / "user_home"
    home.mkdir()
    user_home.mkdir()

    assert load_remote_hosts(home=home) == {}
    assert has_remote_hosts(home=home) is False

    _ = (home / "remote_hosts.toml").write_text(
        '[hosts.zzyasus]\nos = "windows"\ntoken_file = "~/secrets/zzy.tok"\n',
        encoding="utf-8",
    )
    assert has_remote_hosts(home=home) is True
    assert "zzyasus" in load_remote_hosts(home=home)

    tok_path = user_home / "secrets" / "zzy.tok"
    tok_path.parent.mkdir(parents=True)
    _ = tok_path.write_text("  super-secret-token-value-123456  \n", encoding="utf-8")
    assert (
        read_bearer_token("~/secrets/zzy.tok", home=home, user_home=user_home)
        == "super-secret-token-value-123456"
    )

    empty_tok = home / "empty.tok"
    _ = empty_tok.write_text("   \n", encoding="utf-8")
    with pytest.raises(RemoteHostConfigError, match="empty"):
        _ = read_bearer_token("empty.tok", home=home, user_home=user_home)


@pytest.mark.parametrize(
    ("raw_host", "expected"),
    [
        (None, ResolvedTarget("local", "local", "local")),
        ("", ResolvedTarget("local", "local", "local")),
        ("  ", ResolvedTarget("local", "local", "local")),
        ("local", ResolvedTarget("local", "local", "local")),
        ("local://", ResolvedTarget("local", "local", "local")),
        ("ssh://zzyasus", ResolvedTarget("ssh", "zzyasus", "ssh://zzyasus")),
        ("meiva://macmini", ResolvedTarget("meiva", "macmini", "meiva://macmini")),
        ("meiva://ubuntu", ResolvedTarget("local", "local", "local")),
        ("zzyasus", ResolvedTarget("ssh", "zzyasus", "ssh://zzyasus")),
        ("macmini", ResolvedTarget("meiva", "macmini", "meiva://macmini")),
        ("ubuntu", ResolvedTarget("local", "local", "local")),
    ],
)
def test_resolve_target_valid_cases(raw_host: str | None, expected: ResolvedTarget) -> None:
    got = resolve_target(
        raw_host,
        ssh_aliases=("zzyasus",),
        cluster_machines=("macmini", "ubuntu"),
        local_machine="ubuntu",
    )
    assert got == expected


@pytest.mark.parametrize(
    "bad_host",
    [
        "10.20.30.40",
        "127.0.0.1:18792",
        "[::1]",
        "::1",
        "http://127.0.0.1:18792/mcp",
        "https://example.com",
        "file:///etc/passwd",
        "ssh://10.20.30.40",
        "ssh://unregistered",
        "ssh://zzyasus/path",
        "ssh://user@zzyasus",
        "ssh://zzyasus\r",
        "ssh://zzy\nasus",
        "ssh://zzy\x00asus",
        "ssh://zzy\x1fasus",
        "ssh://zzy\x7fasus",
        "ssh://a\\b",
        "ssh://..",
        "ssh://zzyasus..evil",
        "meiva://unknown-box",
        "unknown-box",
    ],
)
def test_resolve_target_rejects_ssrf_bare_ips_and_unregistered(bad_host: str) -> None:
    with pytest.raises(TargetResolutionError, match="meiva computer list"):
        _ = resolve_target(
            bad_host,
            ssh_aliases=("zzyasus",),
            cluster_machines=("macmini", "ubuntu"),
            local_machine="ubuntu",
        )


def test_resolve_target_rejects_ambiguous_short_name() -> None:
    with pytest.raises(TargetResolutionError, match="Ambiguous"):
        _ = resolve_target(
            "sharedbox",
            ssh_aliases=("sharedbox",),
            cluster_machines=("sharedbox",),
        )


def test_is_ip_literal() -> None:
    assert is_ip_literal("10.20.30.40") is True
    assert is_ip_literal("127.0.0.1:18792") is True
    assert is_ip_literal("[::1]") is True
    assert is_ip_literal("zzyasus") is False


@pytest.mark.parametrize("name", ["node;date", "node$var", "%node", "node|suffix"])
def test_registered_cluster_targets_still_require_legal_selector_characters(name: str) -> None:
    with pytest.raises(TargetResolutionError):
        resolve_target(f"meiva://{name}", ssh_aliases=(), cluster_machines=(name,))
