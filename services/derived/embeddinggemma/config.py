# meiva:link services/docs/gateway_side/memory_indexer/embeddinggemma.md

from __future__ import annotations

import argparse
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import get_args

from services.derived.embeddinggemma.contract import (
    Device,
    parse_device,
    require_artifact_sha256,
)

MODULE = "services.derived.embeddinggemma.daemon"
API_KEY_ENV = "MEIVA_EMBEDDINGGEMMA_API_KEY"


@dataclass(frozen=True)
class ServerConfig:
    model_path: str
    device: Device
    revision: str
    model: str
    cache_dir: str
    max_input_length: int
    bind_host: str
    port: int
    litert_path: str | None = None
    require_api_key: bool = False

    def __post_init__(self) -> None:
        require_artifact_sha256(self.revision)
        parse_device(self.device)
        for name, value in (
            ("model path", self.model_path),
            ("model locator", self.model),
            ("cache directory", self.cache_dir),
            ("bind host", self.bind_host),
        ):
            if not value.strip() or value != value.strip():
                raise ValueError(f"EmbeddingGemma {name} must be a non-empty trimmed string")
        if self.max_input_length < 1:
            raise ValueError("EmbeddingGemma max input length must be positive")
        if not 0 < self.port < 65536:
            raise ValueError(f"EmbeddingGemma port {self.port} is out of range")
        if self.litert_path is not None and not self.litert_path.strip():
            raise ValueError("EmbeddingGemma litert path must be omitted or non-empty")


def env_file_value(text: str, name: str) -> str | None:
    value: str | None = None
    for raw in text.splitlines():
        line = raw.strip().removeprefix("export ").lstrip()
        key, separator, rest = line.partition("=")
        if not separator or key.strip() != name:
            continue
        rest = rest.strip()
        if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in {"'", '"'}:
            value = rest[1:-1]
        else:
            value = rest.split(" #", 1)[0].strip()
    return value or None


def api_key_from(environ: Mapping[str, str], env_file_text: str | None) -> str | None:
    key = environ.get(API_KEY_ENV, "").strip()
    if key:
        return key
    return None if env_file_text is None else env_file_value(env_file_text, API_KEY_ENV)


def server_args(config: ServerConfig) -> list[str]:
    args = [
        "--model-path",
        config.model_path,
        "--device",
        config.device,
        "--revision",
        config.revision,
        "--model",
        config.model,
        "--cache-dir",
        config.cache_dir,
        "--max-input-length",
        str(config.max_input_length),
        "--bind-host",
        config.bind_host,
        "--port",
        str(config.port),
    ]
    if config.litert_path is not None:
        args += ["--litert-path", config.litert_path]
    if config.require_api_key:
        args.append("--require-api-key")
    return args


class _Args(argparse.Namespace):
    model_path: str
    device: str
    revision: str
    model: str
    cache_dir: str
    max_input_length: int
    bind_host: str
    port: int
    litert_path: str | None = None
    require_api_key: bool = False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=MODULE)
    for flag in ("model-path", "revision", "model", "cache-dir", "bind-host"):
        _ = parser.add_argument(f"--{flag}", required=True)
    _ = parser.add_argument("--device", required=True, choices=get_args(Device))
    _ = parser.add_argument("--max-input-length", required=True, type=int)
    _ = parser.add_argument("--port", required=True, type=int)
    _ = parser.add_argument("--litert-path")
    _ = parser.add_argument("--require-api-key", action="store_true")
    return parser


def parse_args(argv: Sequence[str]) -> ServerConfig:
    ns = _parser().parse_args(list(argv), namespace=_Args())
    return ServerConfig(
        model_path=ns.model_path,
        device=parse_device(ns.device),
        revision=ns.revision,
        model=ns.model,
        cache_dir=ns.cache_dir,
        max_input_length=ns.max_input_length,
        bind_host=ns.bind_host,
        port=ns.port,
        litert_path=ns.litert_path,
        require_api_key=ns.require_api_key,
    )


def launch_command(config: ServerConfig, *, python: str) -> str:
    return shlex.join([python, "-m", MODULE, *server_args(config)])
