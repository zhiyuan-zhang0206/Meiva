from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

from cli.commands.observability.tests.lgtm.lgtm_harness import LgtmUnit
from cli.commands.observability.tests.lgtm.lgtm_harness import lgtm_unit as lgtm_unit
from cli.commands.observability.tests.observability_inputs import REPO_ROOT

_GRAFANA = REPO_ROOT / "deploy/lgtm/config/grafana"
_SITE_CONTACT = "lgtm/alerting/contact.yml"
_SLOT = "alerting/contact.yml"
_EXAMPLE = _GRAFANA / "contact.agent.example.yml"
_DEFAULT = _GRAFANA / "provisioning" / _SLOT


class _InstalledContact(NamedTuple):
    content: bytes
    hashes: bytes
    contact_inode: int
    hashes_mtime_ns: int


def _installed_contact(unit: LgtmUnit) -> _InstalledContact:
    provisioned = unit.native / "config/provisioning" / _SLOT
    hashes = unit.native / "config/provisioning-hashes.json"
    return _InstalledContact(
        provisioned.read_bytes(),
        hashes.read_bytes(),
        provisioned.stat().st_ino,
        hashes.stat().st_mtime_ns,
    )


def _assert_installed(unit: LgtmUnit, expected: bytes) -> None:
    installed = _installed_contact(unit)
    assert installed.content == expected
    assert json.loads(installed.hashes)[_SLOT] == hashlib.sha256(expected).hexdigest()


def test_site_alert_policy_changes_and_falls_back(lgtm_unit: LgtmUnit) -> None:
    override = lgtm_unit.home / _SITE_CONTACT
    default = _DEFAULT.read_bytes()
    example = _EXAMPLE.read_bytes()

    lgtm_unit.render()
    _assert_installed(lgtm_unit, default)

    override.parent.mkdir(parents=True)
    _ = override.write_bytes(example)
    lgtm_unit.render()
    _assert_installed(lgtm_unit, example)
    lgtm_unit.render()
    _assert_installed(lgtm_unit, example)

    changed = example.replace(b"/agents/1234", b"/agents/4321")
    _ = override.write_bytes(changed)
    lgtm_unit.render()
    _assert_installed(lgtm_unit, changed)

    override.unlink()
    lgtm_unit.render()
    _assert_installed(lgtm_unit, default)


@pytest.mark.parametrize(
    "invalid",
    [
        b"",
        b"contactPoints: [",
        b"\xff",
        b"apiVersion: 1\ncontactPoints: []\npolicies: []\n",
        b"apiVersion: 1\ncontactPoints: [{}]\npolicies: [{}]\n",
    ],
)
def test_invalid_alert_document_preserves_installed_config(
    lgtm_unit: LgtmUnit, invalid: bytes
) -> None:
    lgtm_unit.render()
    previous = _installed_contact(lgtm_unit)
    override = lgtm_unit.home / _SITE_CONTACT
    override.parent.mkdir(parents=True)
    _ = override.write_bytes(invalid)

    with pytest.raises(ValueError, match="Grafana alerting override"):
        lgtm_unit.render()

    assert _installed_contact(lgtm_unit) == previous


@pytest.mark.parametrize(
    ("original", "replacement", "error"),
    [
        ("apiVersion: 1", "apiVersion: 1\napiVersion: 1", "duplicate mapping key"),
        ("    type: webhook", "    type: webhook\n    type: webhook", "duplicate mapping key"),
        ("disableResolveMessage: false", "disableResolveMessage: yes", "must use true or false"),
        (
            "disableResolveMessage: false",
            "disableResolveMessage: !!bool tRuE",
            "must use true or false",
        ),
        ("continue: true", "continue: no", "must use true or false"),
        ("orgId: 1", "orgId: 0x1", "unsigned decimal"),
        ("critical|error", "'['", "invalid regular expression"),
        (
            "      - =~\n      - critical|error",
            "      - '!~'\n      - '['",
            "invalid regular expression",
        ),
        ("critical|error", "'(?=error)'", "unsupported regular expression extension"),
        ("critical|error", "'(error)\\1'", "unsupported regular expression escape"),
        ("critical|error", "'a{1001}'", "repetition exceeds"),
        ("critical|error", "'(a{500}){3}'", "repetition exceeds"),
        ("critical|error", "'a{1}+'", "unsupported regular expression extension"),
        ("group_wait: 30s", "group_wait: 99999999999999999999s", "signed 64-bit"),
        ("group_interval: 5m", "group_interval: 9223372036s855ms", "signed 64-bit"),
        ("repeat_interval: 4h", "repeat_interval: 9223372036855ms", "signed 64-bit"),
        ("group_wait: 30s", "group_wait: 1m1h", "invalid Prometheus duration"),
        ("group_wait: 30s", "group_wait: -1s", "invalid Prometheus duration"),
        ("name: meiva-alerts-webhook", "name: '  '", r"contactPoints\.0\.name"),
        ("receivers:", "receivers: []\n  ignored:", r"contactPoints\.0\.receivers"),
        ("type: webhook", "type: no-such-integration", r"receivers\.0\.type"),
        ("uid: meiva-alerts-webhook", "uid: ''", r"receivers\.0\.uid"),
        ("url: $__env{MEIVA_ALERTS_WEBHOOK_URL}", "url: ''", r"settings\.url"),
        ("  receiver: meiva-alerts-webhook", "  continue: false", "root policy"),
        ("    continue: true", "    continue: 'true'", r"routes\.0\.continue"),
        ("  group_wait: 30s", "  group_wait: nonsense", r"group_wait"),
        ("  group_interval: 5m", "  group_interval: 0s", r"group_interval"),
        ("    - - severity", "    - - 'bad label'", r"object_matchers"),
        ("      - =~", "      - not-equal", r"object_matchers"),
        ("receiver: meiva-agent-webhook", "receiver: missing-point", "must name a contact point"),
    ],
)
def test_invalid_alert_contact_or_route_preserves_installed_config(
    lgtm_unit: LgtmUnit, original: str, replacement: str, error: str
) -> None:
    lgtm_unit.render()
    previous = _installed_contact(lgtm_unit)
    example = _EXAMPLE.read_text(encoding="utf-8")
    assert original in example
    override = lgtm_unit.home / _SITE_CONTACT
    override.parent.mkdir(parents=True)
    _ = override.write_text(example.replace(original, replacement, 1), encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        lgtm_unit.render()

    assert _installed_contact(lgtm_unit) == previous


def test_portable_alert_regex_and_bounded_duration_are_provisioned(lgtm_unit: LgtmUnit) -> None:
    example = _EXAMPLE.read_bytes()
    override = lgtm_unit.home / _SITE_CONTACT
    override.parent.mkdir(parents=True)
    policy = example.replace(b"critical|error", rb"'(?:critical|error)[0-9]{1,4}\.?'").replace(
        b"group_wait: 30s", b"group_wait: 9223372036s854ms"
    )
    _ = override.write_bytes(policy)

    lgtm_unit.render()

    _assert_installed(lgtm_unit, policy)


@pytest.fixture
def configured_site(lgtm_unit: LgtmUnit) -> Path:
    override = lgtm_unit.home / _SITE_CONTACT
    override.parent.mkdir(parents=True)
    _ = override.write_bytes(_EXAMPLE.read_bytes())
    lgtm_unit.render()
    override.unlink()
    return override


@pytest.mark.parametrize("entry", ["file", "parent", "dangling"])
def test_symlink_refuses_before_replacing_installed_config(
    lgtm_unit: LgtmUnit, configured_site: Path, tmp_path: Path, entry: str
) -> None:
    previous = _installed_contact(lgtm_unit)
    outside = tmp_path / "outside"
    outside.mkdir()
    external_file = outside / configured_site.name
    if entry != "dangling":
        _ = external_file.write_bytes(_DEFAULT.read_bytes())
    if entry == "parent":
        configured_site.parent.rmdir()
        configured_site.parent.symlink_to(outside, target_is_directory=True)
    else:
        configured_site.symlink_to(external_file)

    with pytest.raises(ValueError, match="without following links"):
        lgtm_unit.render()

    assert _installed_contact(lgtm_unit) == previous


def test_fifo_refuses_before_replacing_installed_config(
    lgtm_unit: LgtmUnit, configured_site: Path
) -> None:
    previous = _installed_contact(lgtm_unit)
    os.mkfifo(configured_site)
    with pytest.raises(ValueError, match="must be a regular file"):
        lgtm_unit.render()
    assert _installed_contact(lgtm_unit) == previous


def test_oversized_contact_refuses_before_replacing_installed_config(
    lgtm_unit: LgtmUnit, configured_site: Path
) -> None:
    previous = _installed_contact(lgtm_unit)
    _ = configured_site.write_bytes(b"#" * (256 * 1024 + 1))
    with pytest.raises(ValueError, match="exceeds 262144 bytes"):
        lgtm_unit.render()
    assert _installed_contact(lgtm_unit) == previous


def test_agent_policy_example_preserves_the_ingest_policy() -> None:
    default = yaml.safe_load(_DEFAULT.read_text(encoding="utf-8"))
    example = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    agent_receiver = example["contactPoints"].pop()
    (receiver,) = agent_receiver["receivers"]
    assert receiver["disableResolveMessage"] is False
    assert receiver["settings"]["url"] == "$__env{MEIVA_ALERTS_WEBHOOK_URL}/agents/1234"
    assert example["policies"][0]["routes"].pop(0) == {
        "receiver": agent_receiver["name"],
        "object_matchers": [["severity", "=~", "critical|error"]],
        "continue": True,
    }
    assert example == default
