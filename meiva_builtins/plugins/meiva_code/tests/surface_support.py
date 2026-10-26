from typing import Any

from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner
from base.config.service_read import FIELD_DOMAINS, ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from base.packages.tests.model_catalog import inert_keys
from base.paths.home import resolve_meiva_home


def code_registry(*others: tuple[str, PluginContributions]) -> ExtensionRegistry:
    from meiva_builtins.plugins.meiva_code import agent_runtime, plugin

    own = plugin.contribute().merged(agent_runtime.contribute())
    return ExtensionRegistry((("meiva_code", own), *others))


# meiva:note
# Install meiva_code's SDK surface (plus the `others` plugins) over this test's catalog and
# configuration authority, with a sampling policy that never reaches for the gateway; the registry
# of the plugins admitted.
def install_code_surface(
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    *others: tuple[str, PluginContributions],
) -> ExtensionRegistry:
    from meiva.sdk_surface import install

    return install.install(
        code_registry(*others),
        catalog=catalog,
        provider_keys=inert_keys(catalog),
        authority=authority,
        config_images=DiskPluginConfigImages(resolve_meiva_home()),
        sampling=fixed_sampling_owner(),
    )


# meiva:note
# A copy of `authority` whose models carry `fields` (configuration field name to value): the
# configuration an SDK process would have booted with, without touching the process settings.
def authority_with(authority: ConfigAuthority, **fields: Any) -> ConfigAuthority:
    all_domains: Any = authority.all_domains
    runtime_model: Any = authority.runtime
    complete: Any = all_domains.model_copy(deep=True)
    runtime: Any = complete if runtime_model is all_domains else runtime_model.model_copy(deep=True)
    for name, value in fields.items():
        domain = FIELD_DOMAINS[name]
        for model in (complete, runtime):
            if model.has_domain(domain):
                setattr(getattr(model, domain), name, value)
    return ConfigAuthority(runtime, complete, authority.env_path)
