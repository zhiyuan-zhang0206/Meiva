"""`meiva.sdk_surface.install` — the one writer of the `meiva` module.

A plugin declares its SDK surface as values (`PluginContributions`); `install(registry)` applies
every plugin's declaration in registry order and `uninstall()` takes all of it back. These tests pin
what only the whole install can promise: a one-plugin round trip leaves `meiva` exactly as it found
it, a plugin that cannot be applied is rolled back whole and reported (the others stay), the SDK-usage
recorder sits outermost over plugin wraps, and wrap layers stack in plugin order.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

import meiva
from base.agents.context.clients import ClientSet
from base.agents.sdk import call_policy
from base.host.proc import run_bounded
from base.packages.plugin_config_contracts import PluginConfigImages
from base.packages.plugin_config_images import (
    DiskPluginConfigImages,
    PluginConfigChangedError,
    PluginConfigOwner,
)
from base.packages.plugins import flags
from base.packages.plugins.extensions import (
    ExtensionRegistry,
    PluginContributions,
    SdkMember,
    SdkNamespace,
    SdkWrap,
)
from base.packages.plugins.plugin_errors import DuplicateRegistration
from base.paths.home import resolve_meiva_home
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from meiva.sdk_surface import install, metering, process_context, skill_sources, wraps
from meiva.sdk_surface.installation import installed
from meiva.sdk_surface.plugins import (
    InvalidNamespaceNameError,
    PluginNamespaceConflictError,
)

InstallSurface = Callable[..., ExtensionRegistry]

_DISABLED_CHILD = Path(__file__).with_name("child_install_disabled.py")


@pytest.fixture(autouse=True)
def _surface(emitted_events: EmittedEvents) -> Iterator[None]:
    """Start from no installation and bare callables, and leave no installation behind."""
    assert installed() is None
    prior = meiva.context
    clients = ClientSet(pipeline_factory=emitted_events.producer)
    process_context.bind_context(replace(prior, clients=clients))
    try:
        yield
    finally:
        try:
            install.uninstall()
        finally:
            try:
                clients.close()
            finally:
                process_context.bind_context(prior)


@pytest.fixture
def load_failures() -> list[tuple[str, BaseException]]:
    """Every plugin load failure the install reports, as (plugin, exception)."""
    return []


@pytest.fixture
def install_surface(load_failures: list[tuple[str, BaseException]]) -> Iterator[InstallSurface]:
    """The real installer, given a valid sampling owner and a reporter that collects failures."""
    sampling = call_policy.SamplingPolicyOwner(reader=call_policy.SamplingPolicy)

    def collect(name: str, exc: BaseException) -> None:
        load_failures.append((name, exc))

    def install_registry(
        registry: ExtensionRegistry, *, config_images: PluginConfigImages | None = None
    ) -> ExtensionRegistry:
        images = config_images or DiskPluginConfigImages(resolve_meiva_home())
        return install.install(registry, collect, config_images=images, sampling=sampling)

    yield install_registry
    sampling.close()


def _registry(*plugins: tuple[str, PluginContributions]) -> ExtensionRegistry:
    return ExtensionRegistry(plugins)


def test_plugin_overlay_publishes_a_new_owned_image(
    unit_home: Path, install_surface: InstallSurface
) -> None:
    from pydantic import BaseModel, ConfigDict, Field

    from meiva.sdk_surface import settings as sdk_settings

    class Config(BaseModel):
        model_config = ConfigDict(frozen=True)
        marker: str = Field(default="disk", json_schema_extra={"per_agent": True})

    images = DiskPluginConfigImages(unit_home)
    install_surface(
        _registry(("owned", PluginContributions(config=Config))),
        config_images=images,
    )
    before = installed()
    assert before is not None
    assert before.config_images is images
    assert sdk_settings.plugins.owned is before.configs["owned"]

    install.apply_config_overlay({"marker": "agent"})
    after = installed()
    assert after is not None and after is not before
    assert before.configs["owned"].model_dump()["marker"] == "disk"
    assert after.configs["owned"].model_dump()["marker"] == "agent"
    assert sdk_settings.plugins.owned is after.configs["owned"]
    assert after.registry is before.registry
    assert after.config_images is before.config_images


def _namespace(**members: Callable[..., Any]) -> SimpleNamespace:
    return SimpleNamespace(__doc__="An installed test namespace.", **members)


def _ping() -> str:
    """Return pong."""
    return "pong"


def _extra() -> str:
    """Return extra."""
    return "extra"


def _tag(name: str) -> Callable[..., Any]:
    """A wrapper that surrounds the inner result with `name(...)`."""

    def wrapper(inner: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
        return f"{name}({inner(*args, **kwargs)})"

    return wrapper


def _passthrough(inner: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return inner(*args, **kwargs)


_CORE_DIR = Path(inspect.getfile(meiva)).resolve().parent


def _is_core_module(name: str) -> bool:
    """Whether a loaded module is backed by a file of the core `meiva` package."""
    file = getattr(sys.modules.get(name), "__file__", None)
    return file is not None and Path(file).resolve().is_relative_to(_CORE_DIR)


def _surface_snapshot() -> dict[str, Any]:
    """Everything an install may write that a test can observe from outside."""
    return {
        "all": list(meiva.__all_for_meiva__),
        "self_all": list(meiva.self.__all_for_meiva__),
        "files_read": meiva.files.read,
        "self_attrs": set(vars(meiva.self)),
        "wrappers": dict(wraps.wrappers()),
        "roots": skill_sources.roots(),
        "modules": {k for k in sys.modules if k.startswith("meiva.")},
    }


def _assert_surface_unchanged(before: dict[str, Any]) -> None:
    """The surface is what it was before the install.

    A core module that was imported for the first time since `before` (the first skill-root scan
    imports `meiva.skills.skill_index`, whichever test reaches it first on a worker) is a lazy import,
    not an install leftover. Namespaces a plugin registers have no file in the core package, and a
    core module that disappeared is still a difference.
    """
    after = _surface_snapshot()
    after["modules"] = {
        name for name in after["modules"] if name in before["modules"] or not _is_core_module(name)
    }
    assert after == before


# ── one plugin, every piece, fully removed ─────────────────────────────────────


def _assert_namespaces_visible() -> None:
    assert meiva.install_ns.ping() == "w(pong)"  # type: ignore[attr-defined]
    assert meiva.install_ns.extra() == "extra"  # type: ignore[attr-defined]
    assert "extra" in meiva.install_ns.__all_for_meiva__  # type: ignore[attr-defined]
    assert "install_ns" in meiva.__all_for_meiva__
    assert sys.modules["meiva.install_ns"] is meiva.install_ns  # type: ignore[attr-defined]
    assert meiva.self.install_probe() == "extra"  # type: ignore[attr-defined]
    assert "install_probe" in meiva.self.__all_for_meiva__


def _assert_wraps_and_roots(root: Path) -> None:
    assert [p for p, _w in wraps.stack("install_ns.ping")] == ["demo"]
    assert [p for p, _w in wraps.stack("files.read")] == ["demo"]
    assert root in skill_sources.roots()


def test_a_one_plugin_install_is_fully_removed_by_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, install_surface: InstallSurface
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    before = _surface_snapshot()
    registry = _registry(
        (
            "demo",
            PluginContributions(
                sdk_namespaces=(SdkNamespace("install_ns", _namespace(ping=_ping), expand=True),),
                sdk_members=(
                    SdkMember("install_ns", "extra", _extra),
                    SdkMember("self", "install_probe", _extra),
                ),
                sdk_expansions=("shell.sessions",),
                sdk_wraps=(
                    SdkWrap("install_ns.ping", _tag("w")),
                    SdkWrap("files.read", _passthrough),
                ),
                skill_sources=(lambda: [root],),
            ),
        )
    )

    admitted = install_surface(registry)

    assert admitted == registry
    installation = installed()
    assert installation is not None and installation.registry == registry
    _assert_namespaces_visible()
    _assert_wraps_and_roots(root)
    assert install.expansions() == ("install_ns", "shell.sessions")

    install.uninstall()

    assert installed() is None
    assert install.expansions() == ()
    assert not hasattr(meiva, "install_ns")
    assert not hasattr(meiva.self, "install_probe")
    _assert_surface_unchanged(before)


def test_uninstall_without_an_installation_is_a_no_op() -> None:
    before = _surface_snapshot()
    install.uninstall()
    assert installed() is None
    _assert_surface_unchanged(before)


def test_installing_twice_raises_until_uninstall(install_surface: InstallSurface) -> None:
    registry = _registry(
        ("demo", PluginContributions(sdk_namespaces=(SdkNamespace("install_ns", _namespace()),)))
    )
    install_surface(registry)

    with pytest.raises(RuntimeError, match="already installed"):
        install_surface(_registry())

    installation = installed()
    assert installation is not None and installation.registry == registry
    assert hasattr(meiva, "install_ns")

    install.uninstall()
    assert install_surface(_registry()) == _registry()


# ── a plugin the install refuses ───────────────────────────────────────────────


def test_a_second_plugin_declaring_a_taken_namespace_is_refused_and_the_first_stays(
    load_failures: list[tuple[str, BaseException]],
    install_surface: InstallSurface,
) -> None:
    first_ns, second_ns = _namespace(ping=_ping), _namespace(ping=_extra)
    first = PluginContributions(sdk_namespaces=(SdkNamespace("install_ns", first_ns),))
    second = PluginContributions(
        sdk_namespaces=(SdkNamespace("install_ns", second_ns),),
        sdk_members=(SdkMember("self", "second_member", _extra),),
    )

    admitted = install_surface(_registry(("first", first), ("second", second)))

    assert [name for name, _c in admitted.plugins] == ["first"]
    [(name, exc)] = load_failures
    assert name == "second"
    assert isinstance(exc, PluginNamespaceConflictError)
    assert "'first'" in str(exc)
    assert meiva.install_ns.ping() == "pong"  # type: ignore[attr-defined]  # the first plugin's module
    assert not hasattr(meiva.self, "second_member")
    installation = installed()
    assert installation is not None and installation.registry == admitted

    install.uninstall()
    assert not hasattr(meiva, "install_ns")


def _declaring(failing: str) -> PluginContributions:
    """A plugin whose pieces apply in order — namespace, member, expansion, wrap, skill source —
    until the one named by `failing` cannot be applied."""
    base: dict[str, Any] = {
        "sdk_namespaces": (SdkNamespace("rolled_ns", _namespace(ping=_ping), expand=True),),
        "sdk_members": (SdkMember("self", "rolled_member", _extra),),
        "sdk_wraps": (SdkWrap("files.read", _passthrough),),
        "skill_sources": (lambda: [Path("/nonexistent-rolled-skill-root")],),
    }
    if failing == "wrap":
        base["sdk_wraps"] += (SdkWrap("files.does_not_exist", _tag("x")),)
    elif failing == "expansion":
        base["sdk_expansions"] = ("_private",)
    elif failing == "flag":
        base["flags"] = ("bogus",)
    else:
        raise AssertionError(failing)
    return PluginContributions(**base)


@pytest.mark.parametrize(
    ("failing", "error"),
    [
        pytest.param("wrap", wraps.WrapTargetError, id="wrap-target-does-not-resolve"),
        pytest.param("expansion", InvalidNamespaceNameError, id="expansion-path-invalid"),
        pytest.param("flag", flags.UnknownFlag, id="flag-unknown-applied-last"),
    ],
)
def test_a_plugin_whose_later_piece_fails_is_rolled_back_whole(
    load_failures: list[tuple[str, BaseException]],
    failing: str,
    error: type[BaseException],
    install_surface: InstallSurface,
) -> None:
    good = PluginContributions(
        sdk_namespaces=(SdkNamespace("good_ns", _namespace(ping=_ping), expand=True),),
        sdk_wraps=(SdkWrap("good_ns.ping", _tag("g")),),
    )
    before = _surface_snapshot()

    admitted = install_surface(_registry(("bad", _declaring(failing)), ("good", good)))

    [(name, exc)] = load_failures
    assert name == "bad"
    assert isinstance(exc, error)
    assert [n for n, _c in admitted.plugins] == ["good"]
    # Nothing of the bad plugin survives, including the pieces applied before the failing one ...
    assert not hasattr(meiva, "rolled_ns")
    assert "rolled_ns" not in meiva.__all_for_meiva__
    assert "meiva.rolled_ns" not in sys.modules
    assert not hasattr(meiva.self, "rolled_member")
    assert wraps.stack("files.read") == []
    assert not any(str(r).startswith("/nonexistent-rolled") for r in skill_sources.roots())
    assert install.expansions() == ("good_ns",)
    # ... and the plugin after it is installed normally.
    assert meiva.good_ns.ping() == "g(pong)"  # type: ignore[attr-defined]

    install.uninstall()
    _assert_surface_unchanged(before)


# meiva:note
# A namespace the SDK-disable machinery stubbed out is framework-owned: the plugin that declares
# it is refused and the sentinel is not clobbered. The disable swaps a `sys.modules` entry for
# the life of the interpreter, so the scenario runs in a real child.
def test_a_namespace_disabled_by_the_sdk_disable_sentinel_refuses_the_plugin(
    unit_home: Path,
) -> None:
    result = run_bounded(
        [sys.executable, "-I", str(_DISABLED_CHILD)],
        env={**os.environ, "MEIVA_HOME": str(unit_home)},
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "plugin": "wants_disabled",
        "failure": "FrameworkNamespaceConflictError",
        "mentions_disable": True,
        "admitted": [],
        "sentinel_kept": True,
        "namespace_absent": True,
    }


# ── the recorder, wrap layering, views ─────────────────────────────────────────


def test_the_recorder_is_outermost_over_plugin_wraps_and_gone_after_uninstall(
    emitted_events: EmittedEvents,
    install_surface: InstallSurface,
) -> None:
    original_read = meiva.files.read
    registry = _registry(
        (
            "demo",
            PluginContributions(
                sdk_namespaces=(SdkNamespace("install_ns", _namespace(ping=_ping)),),
                sdk_wraps=(
                    SdkWrap("install_ns.ping", _tag("w")),
                    SdkWrap("files.read", _passthrough),
                ),
            ),
        )
    )

    install_surface(registry)

    ping = meiva.install_ns.ping  # type: ignore[attr-defined]
    assert metering.is_recorder(ping)  # the recorder is the outermost layer
    assert not metering.is_recorder(ping.__wrapped__)  # ... with the plugin's layer under it
    assert metering.is_recorder(meiva.files.read)
    assert ping() == "w(pong)"
    # one count per agent call, not one per layer
    assert [event.attributes["fn"] for event in emitted_events] == ["install_ns.ping"]

    install.uninstall()

    # The recorder came off before the plugin wraps were undone, so the framework callable is
    # restored to the very object it was, not to a recorder proxy or a stale chain.
    assert meiva.files.read is original_read
    assert not metering.is_recorder(original_read)
    assert wraps.wrappers() == {}


def test_wrap_layers_stack_in_plugin_order_later_outermost(install_surface: InstallSurface) -> None:
    wrapper_a, wrapper_b, wrapper_c = _tag("a"), _tag("b"), _tag("c")
    registry = _registry(
        (
            "alpha",
            PluginContributions(
                sdk_namespaces=(SdkNamespace("install_ns", _namespace(ping=_ping)),),
                sdk_wraps=(SdkWrap("install_ns.ping", wrapper_a),),
            ),
        ),
        (
            "beta",
            PluginContributions(
                sdk_wraps=(
                    SdkWrap("install_ns.ping", wrapper_b),
                    SdkWrap("install_ns.ping", wrapper_c),
                )
            ),
        ),
    )

    install_surface(registry)

    assert meiva.install_ns.ping() == "c(b(a(pong)))"  # type: ignore[attr-defined]
    # The curated `meiva.extend` views report the same chain, innermost first.
    assert meiva.extend.stack("install_ns.ping") == [
        ("alpha", wrapper_a),
        ("beta", wrapper_b),
        ("beta", wrapper_c),
    ]
    assert meiva.extend.wrappers()["install_ns.ping"] == meiva.extend.stack("install_ns.ping")

    install.uninstall()

    assert meiva.extend.stack("install_ns.ping") == []
    assert "install_ns.ping" not in meiva.extend.wrappers()


def test_expansions_reflect_namespaces_marked_expand_and_declared_paths_in_registry_order(
    install_surface: InstallSurface,
) -> None:
    install_surface(
        _registry(
            (
                "alpha",
                PluginContributions(
                    sdk_namespaces=(
                        SdkNamespace("ns_one", _namespace(), expand=True),
                        SdkNamespace("ns_quiet", _namespace()),
                    ),
                    sdk_expansions=("shell.sessions",),
                ),
            ),
            (
                "beta",
                PluginContributions(
                    sdk_namespaces=(SdkNamespace("ns_two", _namespace(), expand=True),),
                    sdk_expansions=("files",),
                ),
            ),
        )
    )

    # Per plugin: its expanded namespaces, then its explicit paths; a namespace not marked
    # `expand` is installed but not promoted.
    assert install.expansions() == ("ns_one", "shell.sessions", "ns_two", "files")
    assert hasattr(meiva, "ns_quiet")

    install.uninstall()
    assert install.expansions() == ()


def test_install_keeps_core_dependencies_in_the_admitted_declaration(
    install_surface: InstallSurface,
) -> None:
    contribution = PluginContributions(flags=("general.message_timestamps",))

    admitted = install_surface(_registry(("declared", contribution)))

    assert admitted.plugins == (("declared", contribution),)
    installation = installed()
    assert installation is not None and installation.registry == admitted
    install.uninstall()
    assert contribution.flags == ("general.message_timestamps",)


@pytest.mark.parametrize("key", ["bogus.x", "data_plane.db_url"])
def test_invalid_core_dependency_rolls_back_the_plugin(
    load_failures: list[tuple[str, BaseException]], key: str, install_surface: InstallSurface
) -> None:
    bad = PluginContributions(
        sdk_namespaces=(SdkNamespace("failed_ns", _namespace()),),
        flags=("general.message_timestamps", key),
    )
    good = PluginContributions(sdk_namespaces=(SdkNamespace("good_ns", _namespace()),))

    admitted = install_surface(_registry(("bad", bad), ("good", good)))

    assert admitted.plugins == (("good", good),)
    [(name, error)] = load_failures
    assert name == "bad" and isinstance(error, flags.UnknownFlag)
    assert not hasattr(meiva, "failed_ns")
    assert hasattr(meiva, "good_ns")


@dataclass(frozen=True)
class FailingImage(PluginConfigOwner):
    error: BaseException

    def read(self) -> str:
        raise self.error


@dataclass(frozen=True)
class FailingImages(DiskPluginConfigImages):
    error: BaseException

    def owner(self, name: str, cls: type[BaseModel]) -> PluginConfigOwner:
        image = super().owner(name, cls)
        return FailingImage(image.name, image.cls, image.path, self.error)


@pytest.mark.parametrize(
    "error",
    [
        AttributeError("binding implementation bug"),
        ValueError("binding invariant bug"),
        RuntimeError("binding runtime bug"),
        OSError("binding disk failure"),
        PluginConfigChangedError("unhandled config write conflict"),
        DuplicateRegistration("config binding invariant violated"),
        KeyboardInterrupt("binding cancelled"),
    ],
)
def test_unknown_binding_failure_rolls_back_every_plugin_and_propagates_identity(
    load_failures: list[tuple[str, BaseException]],
    error: BaseException,
    install_surface: InstallSurface,
) -> None:
    from pydantic import BaseModel

    before = _surface_snapshot()
    failing = PluginContributions(
        sdk_namespaces=(SdkNamespace("failed_ns", _namespace(ping=_ping)),),
        sdk_members=(SdkMember("self", "failed_member", _extra),),
        sdk_wraps=(SdkWrap("files.read", _passthrough),),
        skill_sources=(lambda: [Path("/nonexistent-failed")],),
        flags=("general.message_timestamps",),
        config=BaseModel,
    )
    prior = PluginContributions(sdk_namespaces=(SdkNamespace("prior_ns", _namespace()),))
    later = PluginContributions(sdk_namespaces=(SdkNamespace("later_ns", _namespace()),))

    with pytest.raises(type(error)) as caught:
        install_surface(
            _registry(("prior", prior), ("failed", failing), ("later", later)),
            config_images=FailingImages(resolve_meiva_home(), error),
        )

    assert caught.value is error
    assert installed() is None
    assert load_failures == []
    _assert_surface_unchanged(before)
    assert not hasattr(meiva, "failed_ns") and not hasattr(meiva, "prior_ns")
    assert not hasattr(meiva, "later_ns")


# meiva:note
# Rollback attempts every undo even when one fails, and a cleanup failure is never success. The
# cleanup fault is real: the failed plugin's namespace is a module whose attribute removal
# refuses, so the member undo raises while the namespace undos behind it must still run. With a
# typed refusal as the primary the cleanup failure is fatal; with an unknown primary failure it
# rides on the original error as a note.
class _StubbornModule(ModuleType):
    def __init__(self, name: str, failure: BaseException) -> None:
        super().__init__(name)
        self.__dict__["__all_for_meiva__"] = []
        self.__dict__["_failure"] = failure

    def __delattr__(self, name: str) -> None:
        if name == "failed_member":
            raise self.__dict__["_failure"]
        super().__delattr__(name)


@pytest.mark.parametrize("refused", [False, True], ids=["unknown-primary", "typed-refusal"])
@pytest.mark.parametrize(
    "cleanup",
    [OSError("member cleanup failed"), KeyboardInterrupt("member cleanup interrupted")],
    ids=["os-error", "cancellation"],
)
def test_rollback_attempts_all_undos_and_cleanup_failure_is_never_success(
    load_failures: list[tuple[str, BaseException]],
    install_surface: InstallSurface,
    refused: bool,
    cleanup: BaseException,
) -> None:
    from pydantic import BaseModel

    primary = RuntimeError("original bind failure")
    before = _surface_snapshot()
    failing = PluginContributions(
        sdk_namespaces=(SdkNamespace("failed_ns", _StubbornModule("failed_ns", cleanup)),),
        sdk_members=(SdkMember("failed_ns", "failed_member", _extra),),
        flags=("not_a_flag",) if refused else (),
        config=None if refused else BaseModel,
    )
    prior = PluginContributions(sdk_namespaces=(SdkNamespace("prior_ns", _namespace()),))
    later = PluginContributions(sdk_namespaces=(SdkNamespace("later_ns", _namespace()),))
    expected = cleanup if refused else primary

    with pytest.raises(type(expected)) as caught:
        install_surface(
            _registry(("prior", prior), ("failed", failing), ("later", later)),
            config_images=FailingImages(resolve_meiva_home(), primary),
        )

    assert caught.value is expected
    if not refused:
        assert any("member cleanup" in note for note in caught.value.__notes__)
    assert load_failures == []
    assert installed() is None
    _assert_surface_unchanged(before)
    assert not hasattr(meiva, "failed_ns") and not hasattr(meiva, "prior_ns")
    assert not hasattr(meiva, "later_ns")
