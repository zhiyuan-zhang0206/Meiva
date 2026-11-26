from __future__ import annotations

import shlex
from dataclasses import replace
from pathlib import Path

from base.cluster.machine import MachineFacts
from base.daemon.health import DaemonProbe
from base.host.system.tests.fake_commands import fake_host
from base.paths import meiva_home
from services.derived.embeddinggemma.config import MODULE, parse_args
from services.derived.embeddinggemma.contract import DEFAULT_MODEL, SHUTDOWN_CEILING_S
from services.derived.embeddinggemma.launch import EmbeddingGemmaSettings
from services.supervision.roster import composition, spec
from services.supervision.roster.embeddinggemma_unit import embeddinggemma_service, probe_host
from services.supervision.roster.service_spec import BOTH

REVISION = "e7a8a2204b91e0f96e92960e84a09a89212e1633dcb7575a9bf3378b4df77f4c"
HOST = "10.64.0.7"
OTHER = "10.64.0.9"


def _settings(**changes: object) -> EmbeddingGemmaSettings:
    base = EmbeddingGemmaSettings(
        host=HOST,
        port=9379,
        device="cpu",
        revision=REVISION,
        model=None,
        model_path="/models/model.litertlm",
        cache_dir="/models/cache",
        python="",
        litert_path="",
        bind_host="0.0.0.0",  # noqa: S104
        max_input_length=8192,
        api_key_configured=False,
    )
    return replace(base, **changes)


def _probe(**_identity: object) -> DaemonProbe:
    return DaemonProbe.up("test")


def _unit(settings: EmbeddingGemmaSettings, *, self_host: str = HOST):
    return embeddinggemma_service(settings, self_host=self_host, probe=_probe)


def _args(cmd: str) -> list[str]:
    words = shlex.split(cmd)
    assert words[1:3] == ["-m", MODULE]
    return words[3:]


def test_the_unit_runs_on_either_kind_of_machine_without_a_database() -> None:
    unit = _unit(_settings())
    assert unit.session == "embeddinggemma"
    assert unit.capabilities == BOTH
    assert unit.requires_db is False
    assert unit.tcp_port == 9379
    assert unit.curl_url is None
    assert unit.identity_probe is not None


def test_the_hosting_machine_renders_the_full_validated_launch() -> None:
    unit = _unit(_settings(litert_path="/opt/litert", api_key_configured=True))
    assert unit.cmd.startswith(".venv/bin/python -m ")
    config = parse_args(_args(unit.cmd))
    assert config.model_path == "/models/model.litertlm"
    assert config.device == "cpu"
    assert config.revision == REVISION
    assert config.model == DEFAULT_MODEL
    assert config.cache_dir == "/models/cache"
    assert config.bind_host == "0.0.0.0"  # noqa: S104
    assert config.port == 9379
    assert config.max_input_length == 8192
    assert config.litert_path == "/opt/litert"
    assert config.require_api_key is True


def test_an_explicit_model_locator_and_interpreter_are_used_verbatim() -> None:
    unit = _unit(_settings(model="/legacy/model.litertlm,cpu", python="/opt/venv/bin/python"))
    assert unit.cmd.startswith("/opt/venv/bin/python -m ")
    assert parse_args(_args(unit.cmd)).model == "/legacy/model.litertlm,cpu"


def test_switching_the_device_changes_only_the_device_in_the_command() -> None:
    cpu = _args(_unit(_settings(device="cpu")).cmd)
    gpu = _args(_unit(_settings(device="gpu")).cmd)
    changed = [(a, b) for a, b in zip(cpu, gpu, strict=True) if a != b]
    assert changed == [("cpu", "gpu")]
    assert cpu[cpu.index("--device") + 1] == "cpu"


def test_an_unknown_device_is_refused_and_never_rendered() -> None:
    assert _unit(_settings(device="cuda")).cmd == f".venv/bin/python -m {MODULE}"
    reason = _gate(HOST, self_host=HOST, device="cuda")
    assert reason is not None
    assert "unsupported EmbeddingGemma device" in reason


def test_another_machine_keeps_a_bare_declaration_and_never_validates_the_launch() -> None:
    bare = _unit(_settings(device="cuda", revision=None), self_host=OTHER)
    assert bare.cmd == f".venv/bin/python -m {MODULE}"
    assert bare.tcp_port == 9379


def test_the_probe_dials_loopback_for_a_wildcard_bind_and_the_bind_address_otherwise() -> None:
    assert probe_host("0.0.0.0") == "127.0.0.1"  # noqa: S104
    assert probe_host("::") == "127.0.0.1"
    assert probe_host("127.0.0.1") == "127.0.0.1"
    assert probe_host("10.64.0.7") == "10.64.0.7"


def test_the_identity_probe_carries_what_the_unit_was_launched_with() -> None:
    seen: dict[str, object] = {}

    def recording(**identity: object) -> DaemonProbe:
        seen.update(identity)
        return DaemonProbe.up("ok")

    unit = embeddinggemma_service(
        _settings(device="gpu", bind_host="10.64.0.7"), self_host=HOST, probe=recording
    )
    assert unit.identity_probe is not None
    assert unit.identity_probe().alive
    assert seen == {
        "url": "http://10.64.0.7:9379/healthz",
        "model": DEFAULT_MODEL,
        "revision": REVISION,
        "device": "gpu",
    }


def _gate(
    host: str, *, self_host: str, revision: str | None = REVISION, **changes: object
) -> str | None:
    unit = next(
        s for s in composition.build_services(home=meiva_home()) if s.session == "embeddinggemma"
    )
    base = composition.gate_settings()
    gate_settings = replace(
        base, embeddinggemma=replace(base.embeddinggemma, host=host, revision=revision, **changes)
    )
    return spec.gate_reason(
        unit,
        settings=gate_settings,
        machine=MachineFacts.of(name="m", role="gateway", host=self_host),
        host=fake_host(),
        home=meiva_home(),
    )


def test_the_default_deployment_has_no_managed_service(unit_home: Path) -> None:
    assert composition.gate_settings().embeddinggemma.host == ""
    reason = _gate("", self_host=HOST)
    assert reason is not None
    assert "MEIVA_EMBEDDINGGEMMA_HOST" in reason


def test_the_start_roster_holds_the_unit_only_on_the_placed_machine(unit_home: Path) -> None:
    assert _gate(HOST, self_host=HOST) is None
    assert _gate(HOST, self_host=OTHER) is not None
    assert _gate(HOST, self_host=HOST, revision=None) is not None


def test_a_misconfigured_hosting_machine_is_refused_with_a_reason_and_still_renders_the_roster() -> (
    None
):
    reason = _gate(HOST, self_host=HOST, bind_host="", max_input_length=0)
    assert reason is not None
    assert reason.startswith("invalid EmbeddingGemma configuration")


def test_every_bad_value_still_declares_a_unit_on_every_machine() -> None:
    for changes in ({"port": 0}, {"port": 70000}, {"bind_host": ""}, {"max_input_length": 0}):
        for self_host in (HOST, OTHER):
            unit = _unit(_settings(**changes), self_host=self_host)
            assert unit.session == "embeddinggemma"
            assert unit.cmd == f".venv/bin/python -m {MODULE}"


def test_the_unit_is_given_time_to_finish_inflight_work_and_close_the_engine() -> None:
    assert _unit(_settings()).stop_ceiling_s == SHUTDOWN_CEILING_S
