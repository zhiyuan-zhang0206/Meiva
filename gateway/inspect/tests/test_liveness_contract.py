import pytest


def _agents_meta_row(liveness_state: str | None) -> tuple[object, ...]:
    config_overlay, machine, status = {}, "machine", "idling"
    last_active_at = last_heartbeat_at = spawned_at = paused_until = None
    last_probe_at = machine_probe_at = None
    return (
        config_overlay,
        machine,
        status,
        last_active_at,
        last_heartbeat_at,
        spawned_at,
        paused_until,
        liveness_state,
        last_probe_at,
        machine_probe_at,
    )


@pytest.mark.parametrize("state", ["online", "offline", "unknown"])
def test_inspector_parses_liveness_at_db_boundary(state: str) -> None:
    from unittest.mock import MagicMock

    from base.api_contracts.agent_contract import LivenessState
    from gateway.inspect._live import db_rows_blocking

    pool = MagicMock()
    cur = pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = [_agents_meta_row(state), (False,)]
    result = db_rows_blocking(pool, 1)
    assert result.liveness_state is LivenessState(state)


@pytest.mark.parametrize("state", [None, "unrecognized"])
def test_inspector_rejects_missing_or_unknown_liveness(state: str | None) -> None:
    from unittest.mock import MagicMock

    from gateway.inspect._live import db_rows_blocking

    pool = MagicMock()
    cur = pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = [_agents_meta_row(state), (False,)]
    with pytest.raises(ValueError):
        db_rows_blocking(pool, 1)
