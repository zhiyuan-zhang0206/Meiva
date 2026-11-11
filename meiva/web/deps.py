# meiva:link meiva/web/docs/web.md

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
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
    now: Callable[[], datetime]


@dataclass(frozen=True)
class SearchDeps:
    provider: SearchProvider
    brave: BraveConfig
    deepseek: DeepSeekSearchConfig
    # meiva:note
    # The process generator the read policy spreads its retries with, built by the root that owns
    # this process (a seeded one in a test) and passed here rather than read where a request is sent.
    # Two bundles are equal when they read the same configuration: the generator is not a fact of
    # the configuration and compares by identity, so it stays out of equality and out of the repr.
    rng: random.Random = field(compare=False, repr=False)


@dataclass(frozen=True)
class FetchDeps:
    reader: ReaderProvider
    jina: JinaConfig
    deepseek: DeepSeekFetchConfig
    fetch_model: FetchModel
    # meiva:note
    # The process generator, as in `SearchDeps`: both deps bundles hand it to `send_json`.
    rng: random.Random = field(compare=False, repr=False)
