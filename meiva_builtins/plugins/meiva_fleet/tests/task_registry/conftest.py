import pytest

from base.config.tests.config_authority import config_authority as config_authority
from base.events.live.bus import EventBus
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from meiva.tests.fixtures.sdk import pinned_bus as pinned_bus


# meiva:note
# The task registry publishes its board refreshes through the event bus of the context that
# commits, so every test here runs with a context that carries one.
@pytest.fixture(autouse=True)
def published_bus(pinned_bus: EventBus) -> EventBus:
    return pinned_bus
