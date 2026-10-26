from __future__ import annotations

import json
import os
import sys
from typing import Any

from base.config import ConfigBoot
from base.config.config_registry import domain_model_classes
from base.host.env.config_lite_table import FIELD_DOMAINS

MODELS = domain_model_classes()

lite = ConfigBoot()
lite.prepare()

results: list[dict[str, Any]] = []
for name, alias, raw in json.loads(os.environ["PARITY_CASES"]):
    os.environ[alias] = raw
    try:
        lite_value = lite.resolve(name)
        lite_error = None
    except Exception as exc:
        lite_value = None
        lite_error = f"{type(exc).__name__}: {exc}"
    model = MODELS[FIELD_DOMAINS[name]]
    try:
        eager_value = getattr(model(), name)
        eager_error = None
        eager_is_value_error = None
    except Exception as exc:
        eager_value = None
        eager_error = f"{type(exc).__name__}: {exc}"
        eager_is_value_error = isinstance(exc, ValueError)
    finally:
        os.environ.pop(alias, None)
    results.append(
        {
            "name": name,
            "alias": alias,
            "raw": raw,
            "lite_value": repr(lite_value),
            "lite_error": lite_error,
            "eager_value": repr(eager_value),
            "eager_error": eager_error,
            "eager_is_value_error": eager_is_value_error,
        }
    )
sys.stdout.write("PARITY " + json.dumps(results) + "\n")
