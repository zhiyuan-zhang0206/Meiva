from __future__ import annotations

import sys
import types

import pytest

from base.db import Database
from base.events.live.bus import EventBus
from tests.fixtures.guards import stub_everywhere


def _stub(_db: Database, _bus: EventBus, *args: object, **kwargs: object) -> None:
    del args, kwargs


def test_alias_scan_does_not_probe_module_getattr(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.agents.impersonation import wake

    touched: list[str] = []

    def _dynamic(name: str) -> None:
        touched.append(name)
        raise AttributeError(name)

    dynamic = types.ModuleType("stub_everywhere_alias_scan_probe")
    dynamic.__getattr__ = _dynamic
    monkeypatch.setitem(sys.modules, dynamic.__name__, dynamic)

    stub_everywhere(monkeypatch, wake, "publish_inbound_wake", _stub)

    assert touched == []


def test_alias_scan_does_not_trigger_the_mcps_servers_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meiva.mcps
    from base.agents.impersonation import wake

    calls: list[str] = []
    real_servers = meiva.mcps.servers

    def _spy() -> list[str]:
        calls.append("servers")
        return real_servers()

    monkeypatch.setattr(meiva.mcps, "servers", _spy)
    stub_everywhere(monkeypatch, wake, "publish_inbound_wake", _stub)

    assert calls == []


def test_alias_scan_still_rebinds_a_frozen_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.agents.impersonation import wake

    real = wake.publish_inbound_wake
    holder = types.ModuleType("stub_everywhere_alias_scan_holder")
    holder.__dict__["publish_inbound_wake"] = real
    monkeypatch.setitem(sys.modules, holder.__name__, holder)

    stub_everywhere(monkeypatch, wake, "publish_inbound_wake", _stub)

    assert holder.__dict__["publish_inbound_wake"] is _stub


def test_driver_batch_preserves_shared_identity_and_repeated_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.fixtures.guards import stub_boundaries

    def original() -> str:
        return "original"

    class First:
        connect = staticmethod(original)

    class Second:
        connect = staticmethod(original)

    class Bound:
        @classmethod
        def connect(cls) -> str:
            return cls.__name__

    descriptor = vars(Bound)["connect"]
    holder = types.ModuleType("driver_batch_shared_identity")
    holder.__dict__.update(connect=original, renamed_connect=original, bound_connect=Bound.connect)
    bound_alias = holder.bound_connect
    monkeypatch.setitem(sys.modules, holder.__name__, holder)
    monkeypatch.setitem(sys.modules, holder.__name__ + "_alias", holder)

    def first() -> str:
        return "first"

    def second() -> str:
        return "second"

    def repeated() -> str:
        return "repeated"

    with pytest.MonkeyPatch.context() as patch:
        stub_boundaries(
            patch,
            (
                (First, "connect", first),
                (Second, "connect", second),
                (First, "connect", repeated),
                (Bound, "connect", second),
            ),
        )
        assert holder.connect() == "repeated"
        assert First.connect() == "repeated"
        assert Second.connect() == "second"
        assert Bound.connect() == "second"
        assert holder.renamed_connect is original
        assert holder.bound_connect is bound_alias

    assert vars(Bound)["connect"] is descriptor
    assert Bound.connect() == "Bound"
    assert holder.connect is original
    assert First.connect is original
    assert Second.connect is original


def test_driver_refusal_finds_new_aliases_without_probing_dynamic_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg
    import redis

    from tests.fixtures.guards import refuse_native_data_plane

    touched: list[str] = []

    def dynamic(name: str) -> None:
        touched.append(name)
        raise AttributeError(name)

    holder = types.ModuleType("driver_batch_late_aliases")
    holder.__getattr__ = dynamic
    monkeypatch.setitem(sys.modules, holder.__name__, holder)
    originals = {"connect": psycopg.connect, "execute_command": redis.Redis.execute_command}
    holder.__dict__.update(originals)

    with pytest.MonkeyPatch.context() as patch:
        refuse_native_data_plane(patch, reason="first driver window")
        for name in originals:
            with pytest.raises(pytest.fail.Exception, match="first driver window"):
                holder.__dict__[name]()

    assert all(holder.__dict__[name] is value for name, value in originals.items())
    late = types.ModuleType("driver_batch_imported_between_windows")
    late.__dict__.update(originals)
    monkeypatch.setitem(sys.modules, late.__name__, late)
    with pytest.MonkeyPatch.context() as patch:
        refuse_native_data_plane(patch, reason="next driver window")
        for module in (holder, late):
            for name in originals:
                with pytest.raises(pytest.fail.Exception, match="next driver window"):
                    module.__dict__[name]()

    assert touched == []
    assert all(late.__dict__[name] is value for name, value in originals.items())


@pytest.mark.parametrize("registered", (False, True))
def test_driver_batch_distinguishes_module_owners(
    monkeypatch: pytest.MonkeyPatch, registered: bool
) -> None:
    from tests.fixtures.guards import stub_boundaries

    def original() -> str:
        return "original"

    def first() -> str:
        return "first"

    def second() -> str:
        return "second"

    owners = [types.ModuleType(f"driver_batch_owner_{index}") for index in range(2)]
    holder = types.ModuleType("driver_batch_module_alias")
    for module in (*owners, holder):
        module.__dict__["connect"] = original
    monkeypatch.setitem(sys.modules, holder.__name__, holder)
    if registered:
        for module in owners:
            monkeypatch.setitem(sys.modules, module.__name__, module)

    with pytest.MonkeyPatch.context() as patch:
        stub_boundaries(patch, ((owners[0], "connect", first), (owners[1], "connect", second)))
        assert holder.connect() == ("second" if registered else "first")
        assert owners[0].connect() == ("second" if registered else "first")
        assert owners[1].connect() == "second"

    assert all(module.connect is original for module in (*owners, holder))
