from __future__ import annotations

import sys

from base.packages import plugin_providers
from base.packages.plugins.config_registration import validate_config_overlay
from base.paths import meiva_home

assert not hasattr(plugin_providers, "_STATE"), "catalog has no ambient holder"
catalog = plugin_providers.build_model_catalog(home=meiva_home())
validate_config_overlay({"llm_model": "deepseek-flash"}, models=catalog.models, home=meiva_home())
sys.stdout.write("ok\n")
