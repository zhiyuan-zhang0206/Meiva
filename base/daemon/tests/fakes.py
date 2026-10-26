from __future__ import annotations

from collections.abc import Callable, Iterator, MutableMapping

import pytest

from base.config import get_field, set_field
from base.host.env.health_ports import HEALTH_PORT_OVERRIDES


class _PortFields(MutableMapping[str, int | None]):
    def __getitem__(self, field: str) -> int | None:
        return get_field(field)

    def __setitem__(self, field: str, value: int | None) -> None:
        set_field(field, value)

    def __delitem__(self, field: str) -> None:
        set_field(field, None)

    def __iter__(self) -> Iterator[str]:
        return iter(HEALTH_PORT_OVERRIDES.values())

    def __len__(self) -> int:
        return len(HEALTH_PORT_OVERRIDES)


def pin_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    *,
    port: Callable[[str], int],
) -> None:
    fields = _PortFields()
    for name, field in HEALTH_PORT_OVERRIDES.items():
        monkeypatch.setitem(fields, field, port(name))
