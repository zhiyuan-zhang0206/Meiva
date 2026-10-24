# meiva:note
# Inbound-message helpers over the test database and event bus, mounted by `path_scopes.toml` on
# the few test files that insert an inbound row or publish its wake. They live with the inbox,
# not in the root harness: building them imports the impersonation wake and the inbox insert,
# which would otherwise sit in every test's dependency set.

from __future__ import annotations

import functools
from collections.abc import Callable

import pytest

from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.inbox.insert import insert_inbound_message
from base.db import Database
from base.events.live.bus import EventBus

__all__ = ["insert_inbound", "publish_wake"]


@pytest.fixture
def publish_wake(database: Database, event_bus: EventBus) -> Callable[[int, str], bool]:
    return functools.partial(publish_inbound_wake, database, event_bus)


@pytest.fixture
def insert_inbound(database: Database, event_bus: EventBus) -> Callable[..., int]:
    return functools.partial(insert_inbound_message, database=database, bus=event_bus)
