from __future__ import annotations

import ast

import pytest

from meizel import allowances
from meizel.owners import decisions


def _parse(source: str) -> ast.Module:
    return ast.parse(source)


# meiva:note
# The table a test judges is a value like the CLI's: these tests name the owners and the exemptions
# they exercise, so they read no checkout and the real policy is free to change.
_POLICY = '[single-owner]\npostgres-dial-owners = ["base/db/connections.py"]\n'
_TABLE = decisions.decision_table(allowances.parse(_POLICY)[0])


_POSITIVE_DIALS = {
    "module_connect": "import psycopg\npsycopg.connect('dsn')\n",
    "aliased_module_connect": "import psycopg as pg\npg.connect('dsn')\n",
    "from_connect_function": "from psycopg import connect\nconnect()\n",
    "aliased_connect_function": "from psycopg import connect as c\nc()\n",
    "from_async_connection_connect": (
        "from psycopg import AsyncConnection\nAsyncConnection.connect()\n"
    ),
    "qualified_async_connection_connect": "import psycopg\npsycopg.AsyncConnection.connect()\n",
    "connection_pool": "from psycopg_pool import ConnectionPool\nConnectionPool()\n",
    "subscripted_aliased_async_connection_pool": (
        "from psycopg_pool import AsyncConnectionPool as P\nP[int]()\n"
    ),
    "qualified_pool_module_connection_pool": (
        "import psycopg_pool\npsycopg_pool.ConnectionPool()\n"
    ),
}


@pytest.mark.parametrize("source", _POSITIVE_DIALS.values(), ids=_POSITIVE_DIALS.keys())
def test_postgres_dial_forms_are_flagged(source: str) -> None:
    sites = decisions.owner_bypasses(_parse(source), "gateway/db.py", (), _TABLE)

    assert sites == {"gateway/db.py::postgres-dial": [2]}


def test_governed_subclass_construction_is_a_dial() -> None:
    source = "from agent.db import LoggingConnectionPool\nLoggingConnectionPool[int]('dsn')\n"

    sites = decisions.owner_bypasses(_parse(source), "gateway/db.py", ("agent", "gateway"), _TABLE)

    assert sites == {"gateway/db.py::postgres-dial": [2]}


def test_ungoverned_subclass_construction_is_not_a_dial() -> None:
    source = "from somepkg.db import LoggingConnectionPool\nLoggingConnectionPool()\n"

    sites = decisions.owner_bypasses(_parse(source), "gateway/db.py", ("agent", "gateway"), _TABLE)

    assert sites == {}


def test_a_locally_defined_pool_subclass_is_a_dial() -> None:
    source = (
        "from psycopg_pool import AsyncConnectionPool\n"
        "class P(AsyncConnectionPool[int]):\n"
        "    pass\n\n\n"
        "P()\n"
    )

    sites = decisions.owner_bypasses(_parse(source), "gateway/db.py", (), _TABLE)

    assert sites == {"gateway/db.py::postgres-dial": [6]}


_NEGATIVE_DIALS = {
    "sqlite3_connect": "import sqlite3\nsqlite3.connect('file.db')\n",
    "connection_from_a_non_psycopg_module": (
        "from mylib import Connection\nConnection.connect()\n"
    ),
    "pool_method_call": (
        "from psycopg_pool import ConnectionPool\nConnectionPool.check_connection('x')\n"
    ),
    "unbound_redis_attribute_pool": "import redis\nredis.ConnectionPool()\n",
    "ungoverned_third_party_pool": "from redis import ConnectionPool\nConnectionPool()\n",
    "unbound_urllib3_https_pool": "import urllib3\nurllib3.HTTPSConnectionPool('h')\n",
    "pool_timeout_from_import": "from psycopg_pool import PoolTimeout\nPoolTimeout()\n",
    "pool_timeout_qualified": "import psycopg_pool\npsycopg_pool.PoolTimeout()\n",
    "bare_annotation": "from psycopg import Connection\ndef f(conn: Connection) -> None: ...\n",
}


@pytest.mark.parametrize("source", _NEGATIVE_DIALS.values(), ids=_NEGATIVE_DIALS.keys())
def test_non_dial_calls_are_not_flagged(source: str) -> None:
    assert decisions.owner_bypasses(_parse(source), "gateway/db.py", (), _TABLE) == {}


def test_the_owner_module_itself_is_exempt() -> None:
    tree = _parse("import psycopg\npsycopg.connect('dsn')\n")

    assert decisions.owner_bypasses(tree, "base/db/connections.py", (), _TABLE) == {}


def test_allowlist_entry_is_stale_once_the_module_stops_bypassing() -> None:
    decision = decisions.Decision(
        owners=frozenset({"base/owner.py"}),
        find=lambda _tree, _roots: [],
        fix="use the owner",
        why="one owner keeps the posture",
        allowed={"gateway/legacy.py": "historical exemption"},
    )

    errors = decisions.allowlist_errors(
        _parse("x = 1\n"), "gateway/legacy.py", (), table={"fake-decision": decision}
    )

    assert len(errors) == 1
    lineno, message = errors[0]
    assert lineno == 1
    assert "stale fake-decision allowlist entry" in message
    assert "remove it from [single-owner.<decision>] in meizel.policy.toml" in message
    assert "an exemption that outlives its reason is a permission nobody re-checks" in message


def test_allowlist_entry_is_not_stale_while_it_still_bypasses() -> None:
    decision = decisions.Decision(
        owners=frozenset({"base/owner.py"}),
        find=lambda _tree, _roots: [3],
        fix="use the owner",
        why="one owner keeps the posture",
        allowed={"gateway/legacy.py": "historical exemption"},
    )

    assert (
        decisions.allowlist_errors(
            _parse("x = 1\n"), "gateway/legacy.py", (), table={"fake-decision": decision}
        )
        == []
    )


def test_allowlist_errors_ignore_modules_outside_the_allowed_map() -> None:
    decision = decisions.Decision(
        owners=frozenset(),
        find=lambda _tree, _roots: [],
        fix="use the owner",
        why="one owner keeps the posture",
        allowed={},
    )

    assert (
        decisions.allowlist_errors(
            _parse("x = 1\n"), "gateway/other.py", (), table={"fake-decision": decision}
        )
        == []
    )


def test_missing_allowlist_errors_flags_a_deleted_allowed_path() -> None:
    decision = decisions.Decision(
        owners=frozenset(),
        find=lambda _tree, _roots: [],
        fix="use the owner",
        why="one owner keeps the posture",
        allowed={"gateway/gone.py": "historical exemption"},
    )

    errors = decisions.missing_allowlist_errors(
        {"gateway/other.py"}, table={"fake-decision": decision}
    )

    assert len(errors) == 1
    assert "gateway/gone.py:1: stale fake-decision allowlist entry" in errors[0]
    assert "the module no longer exists" in errors[0]


def test_missing_allowlist_errors_is_clean_for_an_existing_path() -> None:
    decision = decisions.Decision(
        owners=frozenset(),
        find=lambda _tree, _roots: [],
        fix="use the owner",
        why="one owner keeps the posture",
        allowed={"gateway/present.py": "historical exemption"},
    )

    assert (
        decisions.missing_allowlist_errors(
            {"gateway/present.py"}, table={"fake-decision": decision}
        )
        == []
    )


# meiva:note
# A bypass message is the rule's whole handover: the fact and the repair (asserted above), why the
# decision has one owner, what the site that dials here now owns, and where the rule is written.
# Asserted on fragments, so the wording stays free to change.
def test_a_bypass_message_carries_the_reason_the_owner_and_the_rule_document() -> None:
    message = decisions.site_message("postgres-dial", _TABLE)

    assert message.startswith(
        "bypasses the single owner of `postgres-dial` — dial through the owning module"
    )
    assert "second transport posture" in message
    assert "whoever dials here now owns that posture" in message
    assert "docs/conventions/python-conventions.md" in message


def test_every_decision_carries_a_reason() -> None:
    assert _TABLE
    for name, decision in _TABLE.items():
        assert decision.why.strip(), name
