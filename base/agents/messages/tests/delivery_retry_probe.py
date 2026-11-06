# meiva:note
# The probe test_delivery_retry.py runs in a clean interpreter: the database driver
# is refused at sys.meta_path, the policy must still import and answer, and the
# adapter must be the module that cannot be imported without it.

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys


class _Blocked(importlib.abc.MetaPathFinder):
    def find_spec(
        self, name: str, path: object = None, target: object = None
    ) -> importlib.machinery.ModuleSpec | None:
        if name.split(".", maxsplit=1)[0] in {"psycopg", "psycopg_pool"}:
            raise ImportError(f"{name} is blocked by the probe")
        return None


sys.meta_path.insert(0, _Blocked())
sys.path.insert(0, sys.argv[1])

import base.agents.messages.delivery.retry as policy  # noqa: E402

assert policy.transient_database_failure(
    pool_timeout=False,
    abandoned_restore=False,
    operational_error=True,
    operational_subclass=True,
    sqlstate=None,
)

try:
    import base.agents.messages.delivery.retry_io as _adapter
except ImportError:
    _ = sys.stdout.write("driver confined\n")
else:
    raise AssertionError(f"{_adapter.__name__} imported with psycopg blocked")
