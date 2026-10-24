# meiva:note
# A gateway's cold-start preparation on its own data plane, run for real.
#
# What the preparation is and in which order is `cold_start_plan`'s (`../start_plans`). Here a
# born gateway home goes through `prepare_cold_start`: its own Postgres and Redis come up on the
# recorded ports (`DATA_PLANE`), the gateway schema and the migrations land (`GATEWAY_SCHEMA`,
# `MIGRATIONS`), the completion adopts the delivered gateway login (`POOLER`), and the checks and
# extension steps follow. The preparation runs in a fresh interpreter (`_gateway_cold_start_child`)
# because the birth adopts that login into the process environment, which a test process must not
# carry away. The birth chain with a PgBouncer is `cli/commands/tests/test_single_box.py`.

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg

from base.host.db_authority import delivery, ledger
from base.host.net.url_secret import url_with_userinfo
from cli.commands.data_plane import cluster_instance
from cli.commands.data_plane.tests.data_plane_records import data_plane_config as data_plane_config
from cli.commands.data_plane.tests.native_cluster import NativeCluster
from cli.commands.data_plane.tests.native_cluster import native_cluster as native_cluster
from cli.commands.data_plane.tests.native_cluster import retained_children as retained_children

CHILD = Path(__file__).with_name("_gateway_cold_start_child.py")
REPO_ROOT = Path(__file__).resolve().parents[5]


# meiva:note
# The unit environment a born gateway home delivers to its start: the data plane its record
# names is in the home's `.env`, as `meiva init` writes it, and the process environment is this
# process's own without the keys that `.env` defines, as a service manager's job has none of them.
def _delivered(cluster: NativeCluster) -> dict[str, str]:
    plane = cluster.plane
    declared = {
        "MEIVA_DB_URL": plane.db_url,
        "MEIVA_REDIS_URL": plane.redis_url,
        "MEIVA_REDIS_BIN_DIR": plane.redis_bin_dir,
        "MEIVA_REDIS_ADMIN_PASSWORD": plane.redis_admin_password,
        "MEIVA_CLUSTER_SECRET": plane.cluster_secret,
        "MEIVA_EVENTS_CHANNEL": plane.events_channel,
        "MEIVA_PGBOUNCER_ENABLED": "false",
        "MEIVA_TRUSTED_CIDRS": plane.trusted_cidrs,
        "MEIVA_MACHINE_SERVE_GATEWAY": "true",
    }
    env_file = cluster.home / ".env"
    kept = [
        line for line in env_file.read_text().splitlines() if line.split("=")[0] not in declared
    ]
    env_file.write_text("".join(f"{line}\n" for line in kept))
    with env_file.open("a") as out:
        out.writelines(f"{key}={value}\n" for key, value in declared.items())
    inherited = {k: v for k, v in os.environ.items() if k not in declared}
    return {**inherited, "MEIVA_HOME": str(cluster.home)}


# meiva:note
# A born gateway home goes through its whole cold preparation: its own Postgres and Redis come
# up on the recorded ports, the gateway schema and the migrations land, the completion adopts
# the delivered gateway login, and the checks and extension steps follow, in the plan's order.
def test_a_gateways_preparation_births_its_data_plane_migrates_it_and_adopts_its_login(
    native_cluster: NativeCluster,
) -> None:
    native_cluster.born()

    result = subprocess.run(
        [sys.executable, "-I", "-B", str(CHILD), str(REPO_ROOT)],
        cwd=REPO_ROOT,
        env=_delivered(native_cluster),
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    assert seen["rc"] == 0, result.stderr
    assert seen["phases"] == [
        "converge host",
        "data plane (pg + redis)",
        "gateway schema",
        "migrations",
        "pgbouncer",
        "schema check",
        "extensions adopt",
        "extensions materialize",
    ]
    with psycopg.connect(
        host=str(cluster_instance.pg_socket_dir(native_cluster.home)),
        port=native_cluster.pg_port,
        dbname=native_cluster.identity,
        autocommit=True,
    ) as conn:
        migrated = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()
    assert migrated is not None and migrated[0] > 0, "the migrations landed in the born database"
    secret = ledger.read_secret(
        native_cluster.home, delivery.active_generation(native_cluster.home)
    )
    gateway = secret.roles.of("gateway")
    assert seen["generation"] == "0"
    assert seen["adopted_url"] == url_with_userinfo(
        native_cluster.plane.db_url, gateway.name, gateway.password
    ), "the start process dials the database as the generation's gateway login"
    assert "applied" in result.stdout and "migration(s)" in result.stdout
