# meiva:link meiva/web/docs/web.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from meiva.sdk_surface.injection_check import InjectionCheckInputs
from meiva.web.configs import BraveConfig, DeepSeekFetchConfig, DeepSeekSearchConfig, JinaConfig

__all__ = [
    "FetchDeps",
    "FetchModel",
    "ReaderProvider",
    "SearchDeps",
    "SearchProvider",
    "reader_provider",
    "search_provider",
]


# meiva:note
# The engine behind `search()`. `search_provider` turns the configured `web_search_provider` text into a member.
class SearchProvider(StrEnum):
    BRAVE = "brave"
    DEEPSEEK = "deepseek"


# meiva:note
# The reader behind `fetch()`. `reader_provider` turns the configured `web_fetch_reader` text into a member.
class ReaderProvider(StrEnum):
    JINA = "jina"
    DEEPSEEK = "deepseek"


def _member[Choice: StrEnum](kind: type[Choice], setting: str, value: object) -> Choice:
    try:
        return kind(value)
    except ValueError:
        choices = ", ".join(member.value for member in kind)
        raise ValueError(f"{setting} must be one of {choices}, got {value!r}") from None


def search_provider(value: object) -> SearchProvider:
    return _member(SearchProvider, "web_search_provider", value)


def reader_provider(value: object) -> ReaderProvider:
    return _member(ReaderProvider, "web_fetch_reader", value)


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


@dataclass(frozen=True)
class FetchDeps:
    reader: ReaderProvider
    jina: JinaConfig
    deepseek: DeepSeekFetchConfig
    fetch_model: FetchModel
    injection: InjectionCheckInputs
