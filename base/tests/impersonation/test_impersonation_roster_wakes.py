from base.agents.impersonation import wake
from base.db import Database
from base.events.live.bus import EventBus
from base.events.live.projection import AgentUpdated, ImpersonationChanged
from base.tests.impersonation.publications import Published, tap_publications


def test_impersonation_wake_reconciles_roster_only_for_status_changes(
    database: Database,
    event_bus: EventBus,
) -> None:
    with tap_publications(event_bus, 7) as tap:
        wake.wake_agent(database, event_bus, 7)
        timeline_only = tap.drain()
        wake.wake_agent(database, event_bus, 7, roster_changed=True)
        with_roster = tap.drain()
    assert timeline_only == Published(
        wakes=((7, "impersonation"),), events=(ImpersonationChanged(agent_id=7),)
    )
    assert with_roster == Published(
        wakes=((7, "impersonation"),),
        events=(ImpersonationChanged(agent_id=7), AgentUpdated(agent_id=7)),
    )
