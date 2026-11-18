# meiva:note
# Elect one schedule manager when a machine hosts multiple units.

from dataclasses import dataclass
from pathlib import Path

from psycopg_pool import ConnectionPool

from base.db.rows import query_all


@dataclass(frozen=True)
class _HomeRow:
    home: str


def is_active_unit(pool: ConnectionPool, machine_name: str, home: Path) -> bool:
    with pool.connection() as conn:
        rows = query_all(
            conn,
            _HomeRow,
            "SELECT home FROM machine_units WHERE machine_name = %s AND stopped_at IS NULL ORDER BY serve_gateway DESC, home ASC",
            (machine_name,),
        )
    if not rows:
        return True
    homes = [str(Path(r.home).resolve()) for r in rows]
    my_home = str(Path(home).resolve())
    return my_home == homes[0]
