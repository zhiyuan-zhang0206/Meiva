from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from base.db import Database


class _FakeDatabase:
    def __init__(self, connect: Callable[..., Any]) -> None:
        self.connect = connect


def fake_database(connect: Callable[..., Any]) -> Database:
    return cast(Database, _FakeDatabase(connect))
