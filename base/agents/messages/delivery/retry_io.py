# meiva:note
# The database-driver adapter behind the retry policy: it turns the exception a
# caller already holds into the facts retry.transient_database_failure reads, and
# it is the only module in this package that names psycopg.

from psycopg import OperationalError
from psycopg_pool import PoolTimeout

from base.agents.messages.delivery.retry import transient_database_failure
from base.db.protocols import RestoreAbandonedError

__all__ = ["retryable_database_error"]


def retryable_database_error(error: Exception) -> bool:
    operational = isinstance(error, OperationalError)
    return transient_database_failure(
        pool_timeout=isinstance(error, PoolTimeout),
        abandoned_restore=isinstance(error, RestoreAbandonedError),
        operational_error=type(error) is OperationalError,
        operational_subclass=operational,
        sqlstate=error.sqlstate if operational else None,
    )
