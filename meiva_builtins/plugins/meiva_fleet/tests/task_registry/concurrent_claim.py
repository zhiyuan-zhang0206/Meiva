# meiva:note
# A real lost race for a task title: another transaction has inserted an in_progress task with the
# title but not committed, so the app-level duplicate check cannot see it. `work` runs on its own
# thread and blocks on the unique index until the claim commits, then fails the way a lost race does.
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep

import psycopg

_BLOCKED_ON_LOCK = (
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE datname = current_database() AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
)


def run_against_concurrent_claim[T](db_url: str, title: str, work: Callable[[], T]) -> T:
    with psycopg.connect(db_url) as claimer, psycopg.connect(db_url, autocommit=True) as observer:
        claimer.execute(
            "INSERT INTO agent_tasks (title, description, created_by) VALUES (%s, 'd', 'system')",
            (title,),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(work)
            deadline = monotonic() + 15
            while observer.execute(_BLOCKED_ON_LOCK).fetchone() == (0,):
                assert monotonic() < deadline, "the write never blocked on the concurrent claim"
                assert not running.done(), "the write finished without meeting the concurrent claim"
                sleep(0.02)
            claimer.commit()
            return running.result(timeout=30)
