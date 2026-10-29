from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

from base.lm.catalog.provider_keys import ProviderKeys
from base.lm.decision.call import decide
from base.packages.plugin_config_images import config_image_path
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.plugin_errors import InvalidConfigData
from meiva_builtins.plugins.lm_typesafe.tests.test_local_decision import (
    MODEL,
    QUESTIONS,
    body,
    config_values,
)


def write_config(home: Path, *, wire_model: str) -> Path:
    values = config_values()
    values["local_models"][MODEL]["wire_model"] = wire_model
    path = config_image_path("lm_typesafe", home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values))
    return path


async def verify_snapshots(home: Path) -> None:
    home_one = home / "one"
    home_two = home / "two"
    image = write_config(home_one, wire_model="model-one")
    write_config(home_two, wire_model="model-two")
    one = build_model_catalog(home=home_one)
    two = build_model_catalog(home=home_two)
    write_config(home_one, wire_model="model-three")
    rebuilt = build_model_catalog(home=home_one)
    assert "jev-1.13.0" in one.decision_models
    assert "gpt-6-luna-decisions" in one.decision_models
    assert "gpt-6-luna" in one.models
    assert MODEL not in one.models
    assert "LOCAL_DECISION_API_KEY" in one.key_envs
    sent: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["model"])
        response = body()
        response.pop("routing")
        return httpx.Response(200, json=response)

    for selected in (one, two, rebuilt):
        await decide(
            MODEL,
            "keep the user's requirements",
            QUESTIONS,
            catalog=selected,
            keys=ProviderKeys({}),
            usage_kind="test",
            transport=httpx.MockTransport(respond),
        )
    assert sent == ["model-one", "model-two", "model-three"]
    image.write_text('{"local_endpoint":"invalid","local_models":{}}')
    try:
        build_model_catalog(home=home_one)
    except InvalidConfigData as exc:
        assert "validation" in str(exc)
    else:
        raise AssertionError("bad configured provider published a catalog")
    assert MODEL in one.decision_models
    assert "meiva_builtins.plugins.lm_typesafe.provider" not in sys.modules
    write_config(home_one, wire_model="recovered")
    recovered = build_model_catalog(home=home_one)
    await decide(
        MODEL,
        "still remember",
        QUESTIONS,
        catalog=recovered,
        keys=ProviderKeys({}),
        usage_kind="test",
        transport=httpx.MockTransport(respond),
    )
    assert sent[-1] == "recovered"


def verify_entry_errors(home: Path) -> None:
    provider_dir = home / "plugins" / "z_bad_provider"
    provider_dir.mkdir(parents=True)
    (provider_dir / "plugin.py").write_text("")
    (provider_dir / "meiva-plugin.json").write_text(
        json.dumps(
            {
                "apiVersion": 2,
                "name": "z_bad_provider",
                "version": "1.0.0",
                "engines": {"meiva": ">=2026.10.1"},
                "contributions": {},
            }
        )
    )
    provider_py = provider_dir / "provider.py"
    cases = [
        ("configure = None\ncontribute = None\n", ValueError, "both"),
        ("configure = None\n", TypeError, "callable"),
        ("def configure(config): return None\n", ValueError, "default_config.py"),
    ]
    for source, error, message in cases:
        provider_py.write_text(source)
        try:
            build_model_catalog(home=home)
        except error as exc:
            assert message in str(exc)
        else:
            raise AssertionError("ambiguous or invalid provider entry was admitted")
        assert "plugins.z_bad_provider.provider" not in sys.modules
    (provider_dir / "default_config.py").write_text(
        "from pydantic import BaseModel\n"
        "from base.packages.plugins.extensions import PluginContributions\n"
        "class Config(BaseModel):\n    marker: str = 'present'\n"
        "def contribute(): return PluginContributions(config=Config)\n"
    )
    provider_py.write_text("def configure(config): return None\n")
    try:
        build_model_catalog(home=home)
    except TypeError as exc:
        assert "did not return PluginContributions" in str(exc)
    else:
        raise AssertionError("invalid configure result was admitted")


def main() -> None:
    home = Path(sys.argv[1])
    asyncio.run(verify_snapshots(home))
    verify_entry_errors(home / "invalid-entries")


if __name__ == "__main__":
    main()
