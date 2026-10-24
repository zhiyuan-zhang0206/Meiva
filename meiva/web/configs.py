# meiva:note
# What each provider needs to be called: its key, endpoint, timeout and result cap, read once from
# configuration. Plain values, so `wire.py` can build a request from them.

from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr

__all__ = ["BraveConfig", "DeepSeekFetchConfig", "DeepSeekSearchConfig", "JinaConfig"]


@dataclass(frozen=True)
class BraveConfig:
    api_key: SecretStr | None
    endpoint: str
    timeout_s: float
    max_count: int


@dataclass(frozen=True)
class DeepSeekSearchConfig:
    api_key: SecretStr | None
    endpoint: str
    timeout_s: float
    max_count: int
    qps: float


@dataclass(frozen=True)
class DeepSeekFetchConfig:
    api_key: SecretStr | None
    endpoint: str
    timeout_s: float
    qps: float


@dataclass(frozen=True)
class JinaConfig:
    api_key: SecretStr | None
    reader_base: str
    timeout_s: float
