# meiva:note
# Build the `UnderstandDeps` of this process: its configured models and knobs, the model catalog,
# the bound agent's workspace and output site. `understand_deps_for` takes the configuration and
# the process facts as values; `understand_deps` reads them once from this process's SDK
# installation.

from __future__ import annotations

from pathlib import Path

from base.config.service_read import ConfigAuthority
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.paths import workspace_dir
from meiva import files
from meiva.sdk_surface import agent_identity
from meiva.sdk_surface import settings as sdk_settings
from meiva.understand.answer import OVERFLOW_DIRNAME, OutputSite, RetryPolicy, UnderstandDeps

__all__ = ["understand_deps", "understand_deps_for"]


# meiva:note
# The bound agent's `.exec_output/` site, or None outside an agent process.
def _output_site() -> OutputSite | None:
    try:
        agent_id = agent_identity.require_agent_id()
    except RuntimeError:
        return None
    return OutputSite(workspace_dir(agent_id) / OVERFLOW_DIRNAME, sdk_settings.clock())


# meiva:note
# The deps for one `meiva.understand` call, read from this process's SDK installation.
def understand_deps() -> UnderstandDeps:
    return understand_deps_for(
        sdk_settings.config_authority(),
        catalog=sdk_settings.model_catalog(),
        keys=sdk_settings.provider_keys(),
        overrides=sdk_settings.model_overrides(),
        base_dir=files.workspace_base(),
        output=_output_site(),
    )


def understand_deps_for(
    authority: ConfigAuthority,
    *,
    catalog: ModelCatalog,
    keys: ProviderKeys,
    overrides: ModelOverrides,
    base_dir: Path,
    output: OutputSite | None,
) -> UnderstandDeps:
    return UnderstandDeps(
        catalog=catalog,
        keys=keys,
        overrides=overrides,
        text_model=authority.service_field_value("understand_text_model"),
        media_model=authority.service_field_value("understand_media_model"),
        media_resolution=authority.service_field_value("understand_media_resolution"),
        media_thinking_level=authority.service_field_value("understand_media_thinking_level"),
        media_base_url=authority.service_field_value("understand_media_base_url"),
        llm_override=authority.service_field_value("llm_override"),
        timeout_seconds=authority.service_field_value("llm_invoke_timeout_seconds"),
        retry=RetryPolicy(
            attempts=authority.service_field_value("llm_invoke_retry_attempts"),
            delay_seconds=authority.service_field_value("llm_invoke_retry_delay_seconds"),
            max_delay_seconds=authority.service_field_value("llm_invoke_retry_max_delay_seconds"),
        ),
        base_dir=base_dir,
        output=output,
    )
