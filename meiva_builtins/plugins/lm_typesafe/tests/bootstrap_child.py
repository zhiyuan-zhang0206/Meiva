from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

from base.host.env.bootstrap import BootstrapFetchError
from base.lm.catalog.provider_keys import ProviderKeys
from base.lm.decision.call import decide
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.plugin_errors import InvalidConfigData, SchemaDriftError
from meiva_builtins.plugins.lm_typesafe.tests.test_local_decision import (
    MODEL,
    QUESTIONS,
    body,
)


def require_rejected_projection(home: Path, mode: str) -> None:
    try:
        build_model_catalog(home=home)
    except json.JSONDecodeError:
        assert mode == "malformed"
    except BootstrapFetchError:
        assert mode == "missing"
    except (InvalidConfigData, SchemaDriftError) as exc:
        assert mode != "malformed"
        assert "lm_typesafe" in str(exc) or "plugin cluster config" in str(exc)
    else:
        raise AssertionError("invalid runner projection was admitted")


async def verify() -> None:
    home = Path(sys.argv[1])
    mode = sys.argv[2]
    if mode in {"missing", "malformed", "bad-fields", "bad-values"}:
        require_rejected_projection(home, mode)
        return
    selected = build_model_catalog(home=home)
    if mode == "scratch":
        assert set(selected.decision_models) == {"jev-1.13.0", "gpt-6-luna-decisions"}
        assert not (home / "configs").exists()
        return
    assert MODEL in selected.decision_models
    sent: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["model"])
        assert request.headers["authorization"] == "Bearer delivered-local-key"
        response = body()
        response["routing"] = {"model": "gateway-model"}
        return httpx.Response(200, json=response)

    await decide(
        MODEL,
        "keep the user requirements",
        QUESTIONS,
        catalog=selected,
        keys=ProviderKeys.read(
            selected.key_envs,
            environ={"LOCAL_DECISION_API_KEY": "delivered-local-key"},
            env_file={},
        ),
        usage_kind="test",
        transport=httpx.MockTransport(respond),
    )
    assert sent == ["gateway-model"]


if __name__ == "__main__":
    asyncio.run(verify())
