# meiva:note
# Autouse guards: keep every test hermetic and off the host.
#
# Lightweight autouse fixtures forbid real host effects at their execution
# boundaries (helper converge and os.exec*) or restore
# process-global state (metering, existing telemetry and stdlib logger state).
# CLI and Gateway owners request their heavier readiness, native helper and
# schedule-control guards through ordinary fixture dependencies.
# A leaked environment key,
# `MEIVA_HOME` included, is the root leak guard's: `tests/fixtures/leak_guard.py`. Opt-outs are per-test
# markers documented on the fixture that honours them.


import importlib.abc
import importlib.machinery
import os
import sys
from collections.abc import Callable, Iterator, Sequence
from types import ModuleType
from typing import Any

import psycopg
import pytest
import redis
import redis.asyncio
from redis.asyncio.connection import AbstractConnection as AsyncRedisConnection
from redis.connection import AbstractConnection as RedisConnection

from base.log.tests.stdlib_logging import stdlib_logging_isolated

__all__ = ["refuse_native_data_plane", "stub_everywhere"]


@pytest.fixture(autouse=True)
def suite_is_not_inside_an_exec_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normalize the ambient session to the CI shape (issue #2331).

    A suite launched from a fleet agent's `execute_code` runs inside the call's
    exec-domain session, so `base.host.proc.hosting_exec_domain` reports it and any
    unrelated test that reaches an in-process lifecycle leg (`meiva restart`,
    stop) would refuse — red on an agent box, green
    in CI. A pty-session or login-shell run (the fleet's test convention) never
    sees this. The predicate's own membership behaviour is exercised in spawned
    child processes (`base/host/tests/test_proc.py`), whose sessions are built for
    the case; a lifecycle test that wants the refusal patches this back.
    """
    # Imported here, not at module level: this is a session plugin, and the visibility test
    # needs it to load without pulling base in (`tests/fixtures/unit/tests/test_visibility.py`).
    from base.host import proc

    monkeypatch.setattr(proc, "hosting_exec_domain", lambda: None)


@pytest.fixture(autouse=True)
def _otlp_export_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the test session hermetic: the OTLP dual-write (base.telemetry ->
    base.telemetry.otlp.telemetry_otlp, default ON since the 2026-08-11 stack decision) would
    otherwise fire real OTLP/HTTP requests at 127.0.0.1:4318 from every
    event-emitting test. base/telemetry/tests/otlp/test_telemetry_otlp.py re-enables the
    flag and installs in-memory providers where the OTLP path is under test.
    """
    module = sys.modules.get("base.config")
    if module is not None and "Settings" in vars(module):
        from tests.fixtures.env_bootstrap import permissions_helper_port

        monkeypatch.setattr(module.settings.observability, "telemetry_otlp_enabled", False)
        monkeypatch.setattr(
            module.settings.services, "permissions_helper_port", permissions_helper_port
        )


@pytest.fixture(autouse=True)
def _telemetry_event_store_off() -> Iterator[None]:
    """Restore an existing telemetry sink without loading telemetry for a pure test.

    DB consumers disable the sink before admitting writes. A late telemetry
    producer with no DB dependency remains behind the default driver refusal.
    Drain any pipeline created by this test before its access window closes.
    """
    event_store = sys.modules.get("base.telemetry.event_store")
    if event_store is not None:
        event_store.set_enabled(enabled=False)
    yield
    module = sys.modules.get("base.telemetry.emitter")
    if module is not None:
        module.sync()
        event_store = sys.modules.get("base.telemetry.event_store")
        if event_store is not None:
            event_store.set_enabled(enabled=False)


@pytest.fixture
def sdk_metering() -> Iterator[None]:
    """An unrelated test has no SDK recorder ledger; consumers override this locally."""
    yield


@pytest.fixture(autouse=True)
def _restore_metering(sdk_metering: None) -> None:
    """Per-test isolation for the process-global `meiva` singleton's metering state:
    whatever a test wrapped, the next test sees the bare callables again.

    `meiva.sdk_surface.extensions.load_extensions()` installs the SDK-usage recorder as part of
    the SDK installation (`meiva.sdk_surface.install`), which replaces every public
    `meiva.*` callable — plus the `meiva.mcps._call_raw` MCP funnel — with a recording
    proxy, and nothing ever put them back. `load_extensions()` is reached directly
    *and* lazily, via `meiva/__init__.py:ensure_plugins_loaded` on an `meiva.*` miss, so
    merely touching the namespace permanently swapped out the callables every later
    test in that xdist worker would see.

    That is invisible in isolation and only appears when the polluting test lands in
    the same worker — nondeterministic under `-n`, since `--dist load` distributes
    dynamically. It has already cost a night: a test asserting that `install()` wraps
    an unwrapped funnel read `install()`'s correct no-op as a failure, and the merge
    queue's bisect blamed whichever innocent PR shared the batch (issue #83, after
    #82 fixed that one test's symptom by taking its own baseline).

    Restores from the installation that holds the recorder ledger; a test that
    installs the recorders directly restores its own (``install()`` returns the
    ledger). Free for the tests that never metered: with nothing installed there is
    no ledger and nothing runs. Consumer-local `sdk_metering` carries the SDK
    dependency and teardown; the global default never imports the SDK.
    """
    # The ordinary yield-fixture dependency owns the recorder teardown.
    # Its consumer-local override reads the real installation ledger.


# The old `_stub_os_cron` patched only pytest; registration leaked in the e2e
# gateway subprocess, where monkeypatch could not reach. The inherited
# `MEIVA_OS_JOBS_ENABLED=false` setting covers both processes.


# meiva:note
# Replace `module.name` AND every already-imported alias of it.
#
# Patching only the definition site is a half-guard: `from mod import f` binds
# the function object into the importing module at ITS import time, and that
# alias is what gets called (a caller may hold one for
# `publish_inbound_wake`, `services.supervision.healthchecks.frontend` one for
# `respawn_service`). Nothing is imported to find them — only modules the run
# already loaded are touched.
#
# **This is not a pattern for a new test.** A test does not patch what it wants to control:
# the code under test takes it as a parameter (see "The one allowed form per kind of
# dependency" and "Existing patches" in `docs/conventions/python-conventions.md`). This
# helper serves two jobs that remain: the host guards below, which refuse a host effect
# everywhere it could be reached, and the patches that are still recorded debt, where an
# alias is already frozen because a third-party module or an existing direct import
# retains a function object. The alias scan reads module `__dict__`s only —
# it must not probe `__getattr__` (PEP 562), which can execute arbitrary
# code with side effects (task #3950).
def stub_everywhere(
    monkeypatch: pytest.MonkeyPatch, module: object, name: str, stub: object
) -> None:
    real = getattr(module, name)
    monkeypatch.setattr(module, name, stub)
    for mod in list(sys.modules.values()):
        # Static lookup on purpose — never `getattr(mod, name, None)`: that
        # invokes a module-level `__getattr__` (PEP 562) on every loaded
        # module, and a dynamic surface can run arbitrary code while merely
        # being probed (task #3950: `meiva.mcps.__getattr__` formats its "no such
        # server" message by calling the metered `servers()`, so the old probe
        # emitted a burst of `sdk_call` telemetry rows on every test setup and
        # polluted the per-test event mirror). A dynamically-served name is
        # not a frozen alias either — the real object was never bound into the
        # module's dict, which is exactly the surface the rebind below sets on.
        if getattr(mod, "__dict__", {}).get(name, None) is real:
            monkeypatch.setattr(mod, name, stub)


# meiva:note
# The permissions-helper door is named here and never imported: an import statement would put the
# helper package into every test's dependency set. The refusal activates when the door is first
# imported, whichever test or phase imports it: `_HelperDoorGuard` sits on `sys.meta_path`, wraps
# the door's own loader, and stubs `converge` as soon as the door's body has run, before any
# importer binds the name. `converge` rebuilds, signs and loads the host's launchd job, so a
# forgotten mock must fail the test instead of changing the developer's machine.
_HELPER_DOOR = "services.desktop.permissions_helper"


def _refuse_helper_converge(*_args: object, **_kwargs: object) -> Any:
    pytest.fail(
        "permissions helper native effect forbidden in unit tests: mock the helper "
        "caller or its native command boundary explicitly. Only an isolated native "
        "proof with exact-home cleanup may use @pytest.mark.native_permissions_helper"
    )


# meiva:note
# Finds the door through the other finders and hands back a spec whose loader is guarded.
#
# `arm` gives it the running test's `monkeypatch`, so a door imported mid-test is refused for
# that test and restored when it ends; `disarm` leaves a later import (collection, a
# native-proof test) unrefused, and the autouse fixture refuses the loaded door per test.
class _HelperDoorGuard(importlib.abc.MetaPathFinder):
    def __init__(self) -> None:
        self._patch: pytest.MonkeyPatch | None = None

    def arm(self, patch: pytest.MonkeyPatch) -> None:
        self._patch = patch

    def disarm(self) -> None:
        self._patch = None

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname != _HELPER_DOOR:
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None and spec.loader is not None:
                spec.loader = _GuardedDoorLoader(spec.loader, self)
                return spec
        return None

    def refuse_converge_in(self, door: ModuleType) -> None:
        if self._patch is not None:
            stub_everywhere(self._patch, door, "converge", _refuse_helper_converge)


# meiva:note
# The door's own loader, followed by the refusal for the test that caused the import.
class _GuardedDoorLoader(importlib.abc.Loader):
    def __init__(self, loader: importlib.abc.Loader, guard: _HelperDoorGuard) -> None:
        self._loader = loader
        self._guard = guard

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return self._loader.create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        self._loader.exec_module(module)
        self._guard.refuse_converge_in(module)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._loader, name)


_DOOR_GUARD = _HelperDoorGuard()


def pytest_configure(config: pytest.Config) -> None:
    if _DOOR_GUARD not in sys.meta_path:
        sys.meta_path.insert(0, _DOOR_GUARD)
        config.add_cleanup(lambda: sys.meta_path.remove(_DOOR_GUARD))


@pytest.fixture(autouse=True)
def _guard_permissions_helper_native_io(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Helper ancestry is mandatory at runtime, never an implicit test effect.

    OS_JOBS_ENABLED does not gate the permission ancestor. Caller tests must
    explicitly mock helper converge; helper unit tests may exercise real logic
    with their native command seam mocked. Only an intentional native proof may
    opt out, and remains responsible for its exact-home artifact/job cleanup.

    A door the run has already loaded is refused here; a door first imported while the test
    runs is refused by `_HelperDoorGuard` the moment its body has executed.
    """
    if request.node.get_closest_marker("native_permissions_helper") is not None:
        yield
        return
    door = sys.modules.get(_HELPER_DOOR)
    if door is not None:
        stub_everywhere(monkeypatch, door, "converge", _refuse_helper_converge)
    _DOOR_GUARD.arm(monkeypatch)
    try:
        yield
    finally:
        _DOOR_GUARD.disarm()


@pytest.fixture
def homes_get_their_own_ports(
    monkeypatch: pytest.MonkeyPatch, session_ports: dict[str, int]
) -> None:
    """A home born inside a test records this session's own ports.

    `base.cluster.new_home_ports` is where birth takes the fixed port table
    (`cli.start_identity._new_record`), and those are the numbers the operator's
    cluster on the same box binds. A test home that recorded them could later bring
    up storage, probe or dial the real cluster, and its endpoint would read as the
    real home's (`dotenv_boot._endpoint_key` tells homes apart by port). Every test
    home therefore records the session's kernel-assigned table instead; the table
    itself is pinned by `base/cluster/tests/test_fixed_ports.py`.

    The patch covers this process only: a subprocess that births a home would take
    the real table (no test does)."""
    from base import cluster

    monkeypatch.setattr(cluster, "new_home_ports", lambda: dict(session_ports))


# ── Exec guard: no test replaces the pytest process image ──
#
# `os.exec*` does not raise, does not unwind, and does not run a finalizer — it
# overwrites this process with a new program. A test that reaches one takes the
# whole run with it: pytest never reaches `pytest_sessionfinish`, never prints a
# summary, and the shell sees whatever the *replacement* program exited with.
# There is no failure report, so a full-suite run silently under-reports every
# test that had not run yet.
#
# That is not hypothetical (pre-2026-08-01): `cli.start_refresh.
# refresh_runner_env_or_die` ran from `cli.main.main()` before dispatch on
# `meiva start`, and on an enrolled runner it fetched /api/bootstrap, rewrote the
# real ~/.meiva/.env, and re-exec'd — replacing the pytest process mid-run.
# `cli/tests/test_main_dispatch.py` stubbed it, and the exec guard here is the
# backstop that turns a forgotten stub into a plain test failure instead of a
# vanished run. (The refresh is gone; `meiva start`'s Settings build fetches in-
# meiva:note
# process, and no test reaches a live gateway: the suite pins `MEIVA_CONFIG_FETCH=skip`
# meiva:note
# and an unresolvable gateway URL, and the fetch functions take their environment as a
# meiva:note
# value, so a test dials only the server it starts itself.)
#
# Sibling shape to watch for: the guard is what makes the next one a plain test
# failure instead of a vanished run.


@pytest.fixture(autouse=True)
def _guard_process_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse safety net: the `os.exec*` family fails the test loudly instead of
    replacing the pytest process.

    Patching the four `execv*` names covers all eight — stdlib `os.execl*` are
    thin wrappers that call the module-global `execv`/`execvp`. Real in-process
    exec is never intended from a test: the production call sites are either a
    dedicated `__main__` that runs in a subprocess (`base.native_process.reparent`,
    `services.desktop.browser.daemon`) or a CLI re-exec. Tests
    that assert an exec *would* have happened patch `os.exec*` themselves
    inside the test body — last-write-wins over this default, restored LIFO at
    teardown.

    `os._exit` is deliberately NOT guarded: it is the correct call in a forked
    child (`base.native_process.reparent`), and hijacking it there would resurrect a pytest
    process inside the fork.
    """

    def _refuse(name: str):  # type: ignore[no-untyped-def]
        def _boom(*_args: object, **_kwargs: object) -> object:
            raise AssertionError(
                f"os.{name}() would replace the pytest process image — the run would end "
                f"here with no summary and no failure report. Stub the code path that "
                f"execs (see cli/tests/test_main_dispatch.py), or patch os.{name} in the "
                f"test body if the exec is the subject under test."
            )

        return _boom

    for _name in ("execv", "execve", "execvp", "execvpe"):
        monkeypatch.setattr(os, _name, _refuse(_name))


@pytest.fixture(autouse=True)
def _no_stdlib_telemetry_bridge() -> Iterator[None]:
    """Keep stdlib logging off the loguru->telemetry bridge during tests.

    base/log/__init__.py's `install_stdlib_intercept()` (called by init_gateway_process
    and friends) installs a root-logger handler that forwards stdlib records
    into loguru, whose `_event_sink` turns them into telemetry 'log' events.
    Any test that monkeypatches `base.telemetry.emit` and triggers a log
    record then sees the bridge's forwarded 'log' event in its captured events
    (pgbouncer healthcheck tests, fixed 2026-08-09 via #2136). Tests that
    exercise the bridge itself re-install it inside their body
    (test_uvicorn_stdlib_intercept.py): the fixture runs first, the body's
    install wins for the duration, and teardown restores the saved handlers and
    the logger levels the install changed (`stdlib_logging_isolated`).
    """
    with stdlib_logging_isolated():
        yield


# Save each public/class descriptor before monkeypatching, preserving classmethod
# binding when a resource fixture restores it in its own monkeypatch window.
_POSTGRES_BOUNDARIES: list[tuple[Any, str]] = [(psycopg, "connect")]
for _connection in (psycopg.Connection, psycopg.AsyncConnection):
    _POSTGRES_BOUNDARIES.extend((_connection, name) for name in ("connect", "cursor", "execute"))
for _cursor in (psycopg.Cursor, psycopg.AsyncCursor):
    _POSTGRES_BOUNDARIES.extend((_cursor, name) for name in ("execute", "executemany"))
_REDIS_BOUNDARIES: list[tuple[Any, str]] = [
    (client, "execute_command") for client in (redis.Redis, redis.asyncio.Redis)
]
for _transport in (RedisConnection, AsyncRedisConnection):
    _REDIS_BOUNDARIES.extend((_transport, name) for name in ("connect", "send_packed_command"))

Boundary = tuple[Any, str, Any, Any]


def _save(boundaries: list[tuple[Any, str]]) -> list[Boundary]:
    return [(owner, name, vars(owner)[name], getattr(owner, name)) for owner, name in boundaries]


def _refusal(resource: str, *, reason: str | None = None) -> Callable[..., Any]:
    def forbidden(*_args: object, **_kwargs: object) -> Any:
        pytest.fail(
            reason
            or (
                f"Unrequested {resource} access: request the corresponding resource fixture "
                "(db_url/db_conn/database or redis_url/event_bus) on the real consumer."
            )
        )

    return forbidden


def refuse_native_data_plane(patch: pytest.MonkeyPatch, *, reason: str | None = None) -> None:
    """Install the same driver boundary for an entire static process or a test."""
    for boundaries, resource in ((_POSTGRES_BOUNDARIES, "Postgres"), (_REDIS_BOUNDARIES, "Redis")):
        for owner, name in boundaries:
            forbidden = _refusal(resource, reason=reason)
            stub_everywhere(patch, owner, name, forbidden)


@pytest.fixture(autouse=True)
def native_data_plane_guard(
    monkeypatch: pytest.MonkeyPatch,
    pytestconfig: pytest.Config,
) -> tuple[list[Boundary], list[Boundary]]:
    """Refuse both drivers until an explicit resource dependency admits one."""
    postgres, redis_driver = _save(_POSTGRES_BOUNDARIES), _save(_REDIS_BOUNDARIES)
    reason = (
        "Static tests cannot use Postgres or Redis; use the native test environment."
        if pytestconfig.getoption("test_environment", default="native") == "static"
        else None
    )
    refuse_native_data_plane(monkeypatch, reason=reason)
    return postgres, redis_driver


@pytest.fixture
def postgres_access(
    native_data_plane_guard, process_config: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database fixture's access window; teardown restores the refusal first."""
    for owner, name, original, bound in native_data_plane_guard[0]:
        stub_everywhere(monkeypatch, owner, name, bound)
        monkeypatch.setattr(owner, name, original)


@pytest.fixture
def redis_access(
    native_data_plane_guard, process_config: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Redis fixture's access window, independent of database access."""
    for owner, name, original, bound in native_data_plane_guard[1]:
        stub_everywhere(monkeypatch, owner, name, bound)
        monkeypatch.setattr(owner, name, original)
