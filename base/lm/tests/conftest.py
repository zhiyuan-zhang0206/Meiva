import pytest

from base.lm.catalog import CatalogBuilder, ModelCatalog
from base.log.tests.log_records import loguru_records as loguru_records


@pytest.fixture
def model_catalog() -> ModelCatalog:
    """The core price archive alone: real prices, no plugin bindings.

    The factory's ``MEIVA_LLM_OVERRIDE`` seam returns before model/binding
    resolution, so these tests price their rows from the archive and `base.lm`
    keeps no edge into `base.packages`.
    """
    return CatalogBuilder().build()
