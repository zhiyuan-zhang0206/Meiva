# meiva:link meiva/web/docs/web.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from meiva.sdk_surface.injection_check import InjectionCheckInputs
from meiva.security import FindingDelivery
from meiva.web.configs import BraveConfig, DeepSeekFetchConfig, DeepSeekSearchConfig, JinaConfig

__all__ = [
    "FetchDeps",
    "FetchModel",
    "ReaderProvider",
    "SearchDeps",
    "SearchProvider",
]

SearchProvider = Literal["brave", "deepseek"]
ReaderProvider = Literal["jina", "deepseek"]


@dataclass(frozen=True)
class FetchModel:
    model: str
    reasoning: str
    retry_attempts: int
    retry_delay_seconds: float
    retry_max_delay_seconds: float
    timeout_seconds: float
    llm_override: str
    catalog: ModelCatalog
    keys: ProviderKeys
    overrides: ModelOverrides


@dataclass(frozen=True)
class SearchDeps:
    provider: SearchProvider
    brave: BraveConfig
    deepseek: DeepSeekSearchConfig
    delivery: FindingDelivery


@dataclass(frozen=True)
class FetchDeps:
    reader: ReaderProvider
    jina: JinaConfig
    deepseek: DeepSeekFetchConfig
    fetch_model: FetchModel
    delivery: FindingDelivery
    injection: InjectionCheckInputs
