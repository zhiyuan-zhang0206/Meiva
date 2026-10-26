import pytest

from base.config.tests.config_authority import config_authority as config_authority
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from meiva.sdk_surface.installation import Installation
from meiva.tests.fixtures.sdk import sdk_root as sdk_root


# meiva:note
# The task registry publishes its board refreshes through the SDK's event bus, which draws its
# retry jitter from the generator the SDK root carries; these tests pin an agent without
# installing a surface, so each runs under the bare installation `sdk_root` holds.
@pytest.fixture(autouse=True)
def installed_sdk_root(sdk_root: Installation) -> Installation:
    return sdk_root
