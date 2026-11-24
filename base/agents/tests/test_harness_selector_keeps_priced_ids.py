from base.agents.model.harness_models import resolve_harness_catalog_model
from base.lm.catalog import ModelCatalog


def test_a_harness_selector_never_rewrites_a_priced_model_id(model_catalog: ModelCatalog) -> None:
    prices = model_catalog.prices
    for model in [*prices.archive, *prices.plugin]:
        assert resolve_harness_catalog_model(model, prices) == model
