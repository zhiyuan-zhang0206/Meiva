# meiva:link meiva/web/docs/web.meiva.okf.md

from __future__ import annotations

from typing import cast

from base.config.service_read import ConfigAuthority
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from meiva.sdk_surface import agent_identity
from meiva.sdk_surface import settings as sdk_settings
from meiva.sdk_surface.injection_check import injection_check_inputs
from meiva.security import FindingDelivery, current_delivery
from meiva.web.configs import BraveConfig, DeepSeekFetchConfig, DeepSeekSearchConfig, JinaConfig
from meiva.web.deps import FetchDeps, FetchModel, ReaderProvider, SearchDeps, SearchProvider
from meiva.web.errors import FetchError

__all__ = ["fetch_deps", "fetch_deps_for", "search_deps", "search_deps_for"]


def search_deps() -> SearchDeps:
    return search_deps_for(sdk_settings.config_authority(), delivery=current_delivery())


def search_deps_for(authority: ConfigAuthority, *, delivery: FindingDelivery) -> SearchDeps:
    timeout_s = authority.service_field_value("web_search_timeout_seconds")
    return SearchDeps(
        provider=cast(SearchProvider, authority.service_field_value("web_search_provider")),
        brave=BraveConfig(
            api_key=authority.service_field_value("brave_api_key"),
            endpoint=authority.service_field_value("web_brave_search_endpoint"),
            timeout_s=timeout_s,
            max_count=authority.service_field_value("web_max_search_results"),
        ),
        deepseek=DeepSeekSearchConfig(
            api_key=authority.service_field_value("deepseek_api_key"),
            endpoint=authority.service_field_value("web_deepseek_search_endpoint"),
            timeout_s=timeout_s,
            max_count=authority.service_field_value("web_deepseek_max_results"),
            qps=authority.service_field_value("web_deepseek_qps"),
        ),
        delivery=delivery,
    )


def fetch_deps() -> FetchDeps:
    authority = sdk_settings.config_authority()
    try:
        catalog = sdk_settings.model_catalog()
        keys = sdk_settings.provider_keys()
        overrides = sdk_settings.model_overrides()
    except RuntimeError as exc:
        raise FetchError(
            "web.fetch requires this SDK installation's model configuration (ModelCatalog), "
            "but it carries none; web.search needs no model"
        ) from exc
    return fetch_deps_for(
        authority,
        catalog=catalog,
        keys=keys,
        overrides=overrides,
        delivery=current_delivery(),
        agent_id=agent_identity.agent_id(),
    )


def fetch_deps_for(
    authority: ConfigAuthority,
    *,
    catalog: ModelCatalog,
    keys: ProviderKeys,
    overrides: ModelOverrides,
    delivery: FindingDelivery,
    agent_id: int | None,
) -> FetchDeps:
    fetch_timeout_s = authority.service_field_value("web_fetch_timeout_seconds")
    return FetchDeps(
        reader=cast(ReaderProvider, authority.service_field_value("web_fetch_reader")),
        jina=JinaConfig(
            api_key=authority.service_field_value("jina_api_key"),
            reader_base=authority.service_field_value("web_jina_reader_base"),
            timeout_s=fetch_timeout_s,
        ),
        deepseek=DeepSeekFetchConfig(
            api_key=authority.service_field_value("deepseek_api_key"),
            endpoint=authority.service_field_value("web_deepseek_fetch_endpoint"),
            timeout_s=fetch_timeout_s,
            qps=authority.service_field_value("web_deepseek_qps"),
        ),
        fetch_model=FetchModel(
            model=authority.service_field_value("web_fetch_model"),
            reasoning=authority.service_field_value("web_fetch_reasoning"),
            retry_attempts=authority.service_field_value("llm_invoke_retry_attempts"),
            retry_delay_seconds=authority.service_field_value("llm_invoke_retry_delay_seconds"),
            retry_max_delay_seconds=authority.service_field_value(
                "llm_invoke_retry_max_delay_seconds"
            ),
            timeout_seconds=authority.service_field_value("llm_invoke_timeout_seconds"),
            llm_override=authority.service_field_value("llm_override"),
            catalog=catalog,
            keys=keys,
            overrides=overrides,
        ),
        delivery=delivery,
        injection=injection_check_inputs(
            authority, catalog, keys=keys, agent_id=agent_id, delivery_active=delivery.active
        ),
    )
