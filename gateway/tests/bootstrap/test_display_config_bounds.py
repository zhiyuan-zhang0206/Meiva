from pathlib import Path

import psycopg
import pytest

from base.config import runtime_config
from base.config.domains.display import DisplaySettings
from base.config.domains.tests.test_display_config import DISPLAY_RANGES
from base.host.env import dotenv_file
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.events.test_notices_endpoint import seed_agent


@pytest.mark.parametrize(("name", "minimum", "maximum"), DISPLAY_RANGES)
def test_config_write_rejects_invalid_default_without_changing_file(
    unit_home: Path, name: str, minimum: int, maximum: int
) -> None:
    runtime_config.write_fields({"notices_open_default_limit": 200}, set())
    env_path = unit_home / ".env"
    before = env_path.read_bytes()

    with gateway_test_client() as client:
        for value in (minimum - 1, maximum + 1):
            response = client.put("/api/config", json={name: value})
            assert response.status_code == 400, response.text
            assert env_path.read_bytes() == before


@pytest.mark.parametrize(("name", "minimum", "maximum"), DISPLAY_RANGES)
def test_config_write_accepts_boundary_defaults_without_clamping(
    unit_home: Path, name: str, minimum: int, maximum: int
) -> None:
    del unit_home
    alias = DisplaySettings.model_fields[name].alias
    assert isinstance(alias, str)
    with gateway_test_client() as client:
        for value in (minimum, maximum):
            response = client.put("/api/config", json={name: value})
            assert response.status_code == 200, response.text
            assert dotenv_file.read_env_aliases()[alias] == str(value)


@pytest.mark.parametrize(("open_limit", "resolved_limit"), ((1, 1), (500, 100)))
def test_implicit_notice_reads_use_validated_boundary_defaults(
    db_conn: psycopg.Connection,
    open_limit: int,
    resolved_limit: int,
) -> None:
    configured = DisplaySettings.model_validate(
        {"notices_open_default_limit": open_limit, "notices_resolved_default_page": resolved_limit}
    )
    agent_id = seed_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO agent_notices
                (agent_id, local_id, title, priority, require_response, expire_at)
            SELECT %s, i, 'open', 'P3', true, now() + interval '1 day'
            FROM generate_series(1, 601) i""",
            (agent_id,),
        )
        cur.execute(
            """INSERT INTO agent_notices
                (agent_id, local_id, title, priority, require_response,
                 resolved_at, resolution, expire_at)
            SELECT %s, i + 1000, 'resolved', 'P3', true, now(), 'dismissed', now() + interval '1 day'
            FROM generate_series(1, 101) i""",
            (agent_id,),
        )
    db_conn.commit()

    with gateway_test_client() as client:
        client.app.state.notices_open_default_limit = configured.notices_open_default_limit
        client.app.state.notices_resolved_default_page = configured.notices_resolved_default_page
        for path, expected in (
            ("/api/notices/open", open_limit),
            ("/api/notices/live", open_limit),
        ):
            implicit = client.get(path)
            explicit = client.get(path, params={"limit": expected})
            assert implicit.status_code == explicit.status_code == 200
            assert len(implicit.json()) == expected
            assert implicit.json() == explicit.json()
        feed = client.get("/api/notices")
        assert feed.status_code == 200
        assert feed.json()["open"] == []
        assert len(feed.json()["awaiting"]) == open_limit
        assert len(feed.json()["resolved_page"]) == resolved_limit
