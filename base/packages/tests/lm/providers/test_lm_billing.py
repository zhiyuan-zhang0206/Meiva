# meiva:note
# Billing schema assertions use real process-owned tracing; vendor lookup stays in process.
# meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md

from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from base.lm.catalog import ModelCatalog
from base.lm.provider_api import ProviderBinding
from base.packages.tests.lm.providers.lm_process import run_billing
from base.packages.tests.model_catalog import AddBindings


def test_emit_billing_event_records_schema_v1_attributes(tmp_path: Path) -> None:
    run_billing(tmp_path, "schema", "message", "none")


@pytest.mark.parametrize("path", ["message", "durable"])
def test_message_billing_accounts_for_both_cache_write_ttls(tmp_path: Path, path: str) -> None:
    run_billing(tmp_path, "cache_write_ttls", path, "none")


def test_emit_billing_from_message_marks_unpriced_model(tmp_path: Path) -> None:
    run_billing(tmp_path, "unpriced", "message", "none")


def test_emit_billing_from_message_skips_missing_usage_metadata(tmp_path: Path) -> None:
    run_billing(tmp_path, "missing_usage", "message", "none")


@pytest.mark.parametrize(
    ("model", "vendor"),
    [
        ("claude-sonnet-4", "anthropic"),
        ("deepseek-v4-pro", "deepseek"),
        ("gemini-3.5-flash", "google"),
        ("gpt-5.6-sol", "openai"),
        ("mimo-v2", "xiaomi"),
        ("kimi-k2.7-code", "moonshot"),
        ("glm-5", "zhipu"),
        ("qwen3.8-max", "alibaba"),
        # meiva:note
        # Alibaba family ids nest directly after the stem; a bare dash prefix
        # would miss them, and a non-Alibaba "qwenfoo-*" must not be attributed.
        ("qwenfoo-fast", None),
        ("unregistered-model", None),
    ],
)
def test_vendor_of_model_recognizes_registered_core_and_plugin_prefixes(
    model_catalog: ModelCatalog,
    model: str,
    vendor: str | None,
) -> None:
    # meiva:note
    # Vendor attribution uses registered manufacturer identities without guessing.
    # meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md
    from base.lm.usage import vendor_of_model

    assert vendor_of_model(model, catalog=model_catalog) == vendor


def test_vendor_of_model_uses_registered_plugin_display_name(
    model_catalog: ModelCatalog, add_bindings: AddBindings
) -> None:
    # meiva:note
    # A registered plugin provider contributes its lowercase manufacturer name.
    #
    # The regression this catches is silently skipping a billable plugin call
    # after provider registration has made the model invokable.
    #
    # meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md
    from base.lm.usage import vendor_of_model

    model_catalog = add_bindings(
        model_catalog,
        {
            "acme-": ProviderBinding(
                prefix="acme-",
                display_name="Acme AI",
                key_env="ACME_API_KEY",
                build=lambda _ctx: FakeListChatModel(responses=["unused"]),
            )
        },
    )

    assert vendor_of_model("acme-fast", catalog=model_catalog) == "acme ai"


def test_emit_billing_event_is_noop_when_tracing_is_disabled(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MEIVA_TRACE_ENABLED=false\n")
    run_billing(tmp_path, "disabled", "message", "none")


def test_emit_billing_event_absorbs_tracing_failure(tmp_path: Path) -> None:
    run_billing(tmp_path, "failure", "message", "none")


@pytest.mark.parametrize("path", ["message", "durable"])
@pytest.mark.parametrize("cached", [None, 0])
def test_billing_distinguishes_unmetered_cache_from_reported_zero(
    tmp_path: Path, path: str, cached: int | None
) -> None:
    run_billing(tmp_path, "cache_read", path, "none" if cached is None else str(cached))
