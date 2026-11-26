from __future__ import annotations

from dataclasses import replace

import pytest

from services.derived.embeddinggemma import config as server_config
from services.derived.embeddinggemma import contract, placement
from services.derived.embeddinggemma.tests.fakes import REVISION

SELF = "10.64.0.7"


@pytest.mark.parametrize(("value", "expected"), [("cpu", "cpu"), (" GPU ", "gpu")])
def test_device_is_parsed_case_insensitively(value: str, expected: str) -> None:
    assert contract.parse_device(value) == expected


@pytest.mark.parametrize("value", ["", "cuda", "tpu", "cpu,gpu"])
def test_an_unknown_device_fails_fast(value: str) -> None:
    with pytest.raises(ValueError, match="unsupported EmbeddingGemma device"):
        contract.parse_device(value)


@pytest.mark.parametrize("revision", [None, "", "A" * 64, "a" * 40, "a" * 63])
def test_the_server_pins_only_a_lowercase_artifact_sha256(revision: str | None) -> None:
    with pytest.raises(ValueError, match="64-hex SHA256"):
        contract.require_artifact_sha256(revision)


def test_placement_matches_the_machines_own_reachable_address() -> None:
    assert placement.hosted_here(SELF, self_host=SELF)
    assert placement.hosted_here(" [::1] ", self_host="::1")
    assert placement.hosted_here("Mac-Mini", self_host="mac-mini")
    assert not placement.hosted_here("10.64.0.2", self_host=SELF)
    assert not placement.hosted_here("", self_host="")
    for alias in ("127.0.0.1", "::1", "[::1]", "localhost", "LOCALHOST", "ip6-localhost"):
        assert placement.hosted_here(alias, self_host="127.0.0.1"), alias
        assert placement.hosted_here(alias, self_host=""), alias
    assert not placement.hosted_here("127.0.0.1", self_host=SELF)
    assert placement.hosted_here("localhost", self_host="")
    assert not placement.hosted_here(SELF, self_host="")


def test_the_hosting_machine_dials_loopback_and_others_dial_the_placement_address() -> None:
    assert (
        placement.managed_endpoint(SELF, 9379, self_host=SELF)
        == "http://127.0.0.1:9379/v1/embeddings"
    )
    assert (
        placement.managed_endpoint(SELF, 9379, self_host="10.64.0.3")
        == f"http://{SELF}:9379/v1/embeddings"
    )
    assert (
        placement.managed_endpoint("fd00::7", 9400, self_host="10.64.0.3")
        == "http://[fd00::7]:9400/v1/embeddings"
    )


def test_an_explicit_endpoint_and_model_beat_the_placement() -> None:
    explicit = ("http://external:1/v1/embeddings", "/legacy/model.litertlm,gpu")
    assert (
        placement.client_target(
            endpoint=explicit[0], model=explicit[1], host=SELF, port=9379, self_host=SELF
        )
        == explicit
    )


def test_a_placement_supplies_the_missing_endpoint_and_the_device_free_default_model() -> None:
    endpoint, model = placement.client_target(
        endpoint=None, model=None, host=SELF, port=9379, self_host="10.64.0.3"
    )
    assert endpoint == f"http://{SELF}:9379/v1/embeddings"
    assert model == contract.DEFAULT_MODEL
    assert "cpu" not in model
    assert "gpu" not in model
    assert "/" not in model


def test_without_a_placement_nothing_is_invented() -> None:
    assert placement.client_target(
        endpoint=None, model=None, host="", port=9379, self_host=SELF
    ) == (None, None)
    assert placement.client_target(
        endpoint="http://x/v1/embeddings", model=None, host=" ", port=9379, self_host=SELF
    ) == ("http://x/v1/embeddings", None)


def _config(**changes: object) -> server_config.ServerConfig:
    base = server_config.ServerConfig(
        model_path="/m/model.litertlm",
        device="gpu",
        revision=REVISION,
        model="embeddinggemma-2",
        cache_dir="/m/cache",
        max_input_length=4096,
        bind_host="0.0.0.0",  # noqa: S104
        port=9400,
        litert_path="/opt/litert",
        require_api_key=True,
    )
    return replace(base, **changes)


def test_arguments_round_trip_the_whole_configuration() -> None:
    config = _config()
    assert server_config.parse_args(server_config.server_args(config)) == config
    minimal = _config(litert_path=None, require_api_key=False)
    args = server_config.server_args(minimal)
    assert "--litert-path" not in args
    assert "--require-api-key" not in args
    assert server_config.parse_args(args) == minimal


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": "a" * 40},
        {"device": "cuda"},
        {"model_path": " "},
        {"model": ""},
        {"max_input_length": 0},
        {"port": 0},
        {"port": 70000},
        {"litert_path": ""},
    ],
)
def test_an_invalid_configuration_is_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _config(**changes)


def test_an_unknown_device_flag_ends_the_launch() -> None:
    args = server_config.server_args(_config())
    args[args.index("--device") + 1] = "cuda"
    with pytest.raises(SystemExit):
        server_config.parse_args(args)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("MEIVA_EMBEDDINGGEMMA_API_KEY=plain", "plain"),
        ("MEIVA_EMBEDDINGGEMMA_API_KEY='quoted value'", "quoted value"),
        ('export MEIVA_EMBEDDINGGEMMA_API_KEY="dq"', "dq"),
        ("MEIVA_EMBEDDINGGEMMA_API_KEY=abc # trailing", "abc"),
        ("# MEIVA_EMBEDDINGGEMMA_API_KEY=commented", None),
        ("MEIVA_EMBEDDINGGEMMA_API_KEY_OTHER=x", None),
        ("MEIVA_EMBEDDINGGEMMA_API_KEY=", None),
        ("A=1\nMEIVA_EMBEDDINGGEMMA_API_KEY=first\nMEIVA_EMBEDDINGGEMMA_API_KEY=last", "last"),
    ],
)
def test_the_api_key_is_read_from_env_file_text(text: str, expected: str | None) -> None:
    assert server_config.env_file_value(text, "MEIVA_EMBEDDINGGEMMA_API_KEY") == expected


def test_the_environment_beats_the_file_and_a_blank_value_falls_through() -> None:
    name = "MEIVA_EMBEDDINGGEMMA_API_KEY"
    assert server_config.api_key_from({name: " e "}, f"{name}=f") == "e"
    assert server_config.api_key_from({name: " "}, f"{name}=f") == "f"
    assert server_config.api_key_from({}, None) is None
