from collections.abc import Generator

import pytest

from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner
from base.config.tests.config_authority import config_authority as config_authority
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from meiva.sdk_surface.installation import Installation
from meiva.sdk_surface.tests.installation_holder import held_installation


# meiva:note
# The task registry publishes its board refreshes through the SDK's event bus, which draws its
# retry jitter from the generator the SDK root carries; these tests pin an agent without
# installing a surface, so each runs under a bare installation over a fixed sampling owner.
@pytest.fixture(autouse=True)
def sdk_root() -> Generator[Installation]:
    with held_installation(Installation.bare(sampling=fixed_sampling_owner())) as installation:
        yield installation
