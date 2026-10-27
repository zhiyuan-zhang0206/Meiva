"""The standard `/healthz` daemon entry: derivation, construction-time validation,
and the invariants every such roster entry must keep.

`healthz_daemon` is the one place a launch command, probe URL and identity probe are
derived from `(session, module)`. The invariants below are properties of the whole
roster, not of one service, so a new daemon added later is judged without a new test
line: its rendered unit, its names and its daemon-side literals either agree or one of
these names the entry that does not.
"""

from __future__ import annotations

import ast
import importlib.util
import math
from collections import Counter
from functools import partial
from pathlib import Path
from typing import cast

import pytest

from base.cluster.machine import MachineRole
from base.daemon.endpoints import ServiceEndpoints
from base.daemon.health import DaemonProbe
from base.daemon.tests.fakes import pin_endpoints
from base.host.env.health_ports import DEFAULT_PORTS
from base.native_process.stop_timing import HOST_STOP_CEILING_S, OPS_STOP_CEILING_S, STOP_MARGIN_S
from base.packages.plugin_config_admin import update_all_disk_images
from base.packages.plugins.enable_config import discover_plugins
from base.paths import meiva_home
from services.supervision import roster
from services.supervision.meiva_root_glue import manifests as gen
from services.supervision.roster import composition
from services.supervision.roster.healthz import (
    daemon_identity,
    health_name_of,
    healthz_daemon,
    healthz_url,
)
from services.supervision.roster.service_spec import ServiceSpec

_REPO = Path(__file__).resolve().parents[4]
_PYTHON_M = ".venv/bin/python -m "

_GATEWAY: frozenset[MachineRole] = frozenset({"gateway"})
_RUNNER: frozenset[MachineRole] = frozenset({"agent-runner"})
_BOTH: frozenset[MachineRole] = _GATEWAY | _RUNNER


def _healthz_specs() -> dict[str, ServiceSpec]:
    return {
        s.session: s
        for s in composition.build_services(home=meiva_home())
        if s.health_name is not None
    }


def _module(spec: ServiceSpec) -> str:
    assert spec.cmd.startswith(_PYTHON_M), spec.cmd
    return spec.cmd.removeprefix(_PYTHON_M)


def _daemon_probe(spec: ServiceSpec) -> partial[DaemonProbe]:
    """The standard daemon declares its protocol probe directly."""
    return cast("partial[DaemonProbe]", spec.identity_probe)


# ── the factory ──


def test_the_factory_derives_command_url_probe_and_health_name() -> None:
    spec = healthz_daemon(
        "delivery-watchdog", module="services.x.daemon", capabilities=_GATEWAY, requires_db=True
    )

    assert spec.session == "delivery-watchdog"
    assert spec.health_name == "delivery_watchdog"
    assert spec.cmd == ".venv/bin/python -m services.x.daemon"
    assert (
        spec.curl_url
        == f"http://localhost:{ServiceEndpoints.from_settings().of('delivery_watchdog').health_port}/healthz"
    )
    probe = cast("partial[DaemonProbe]", spec.identity_probe)
    assert probe.func.__name__ == "probe_daemon"
    assert probe.args == ("delivery_watchdog", spec.curl_url)
    assert probe.keywords == {
        "pidfile": ServiceEndpoints.from_settings().of("delivery_watchdog").pidfile
    }


def test_the_factory_passes_the_optional_declarations_through() -> None:
    def gate() -> str | None:
        return None

    spec = healthz_daemon(
        "labeler",
        module="services.derived.labeler.daemon",
        capabilities=_GATEWAY,
        requires_db=False,
        gate=gate,
        profile="agent",
        no_profile_marker=True,
        db_access="gateway",
        stop_ceiling_s=12.5,
    )

    assert (spec.gate, spec.profile, spec.no_profile_marker) == (gate, "agent", True)
    assert (spec.db_access, spec.stop_ceiling_s, spec.requires_db) == ("gateway", 12.5, False)


def test_the_url_follows_the_endpoint_table_resolved_when_the_roster_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_health_port(_name: str) -> int:
        return 18111

    pin_endpoints(monkeypatch, port=_fake_health_port)
    assert healthz_url("im_bridge") == "http://localhost:18111/healthz"
    spec = healthz_daemon("im-bridge", module="a.b", capabilities=_GATEWAY, requires_db=True)
    assert spec.curl_url == "http://localhost:18111/healthz"


@pytest.mark.parametrize(
    "session", ["Heartbeat", "heart_beat", "heart beat", "-heartbeat", "", "1a"]
)
def test_a_session_that_is_not_lowercase_kebab_is_refused(session: str) -> None:
    with pytest.raises(ValueError, match="kebab"):
        health_name_of(session)


@pytest.mark.parametrize(
    "module", ["", "services/x/daemon", "services..x", "a b", "-m evil", "a.b;c"]
)
def test_a_module_that_is_not_a_dotted_path_is_refused(module: str) -> None:
    with pytest.raises(ValueError, match="dotted module path"):
        healthz_daemon("heartbeat", module=module, capabilities=_GATEWAY, requires_db=True)


def test_a_service_without_a_port_slot_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="no port slot") as raised:
        healthz_daemon("not-a-daemon", module="a.b", capabilities=_GATEWAY, requires_db=True)
    assert "not_a_daemon" in str(raised.value)


# ── construction-time validation of every ServiceSpec ──


def _spec(**fields: object) -> ServiceSpec:
    base: dict[str, object] = {
        "session": "x",
        "cmd": "x",
        "capabilities": _GATEWAY,
        "requires_db": False,
    }
    return ServiceSpec(**{**base, **fields})  # pyright: ignore[reportArgumentType]


def test_a_well_formed_spec_constructs() -> None:
    assert _spec().session == "x"
    assert _spec(curl_url="http://localhost:1/", stop_ceiling_s=0.5).stop_ceiling_s == 0.5
    assert _spec(capabilities=_BOTH, requires_db=True, db_access="gateway").db_access == "gateway"
    assert _spec(tcp_port=65535).tcp_port == 65535
    assert _spec(curl_url="http://localhost:1/__meiva/healthz", home_healthz=True).home_healthz


_MALFORMED: list[tuple[dict[str, object], str]] = [
    ({"capabilities": frozenset()}, "no capabilities"),
    ({"capabilities": frozenset({"gateway", "mainframe"})}, "unknown capabilities"),
    ({"curl_url": "http://localhost:1/", "tcp_port": 1}, "both curl_url and tcp_port"),
    ({"tcp_port": 0}, "invalid tcp_port"),
    ({"tcp_port": 65536}, "invalid tcp_port"),
    ({"stop_ceiling_s": 0}, "positive finite"),
    ({"stop_ceiling_s": -1.0}, "positive finite"),
    ({"stop_ceiling_s": math.inf}, "positive finite"),
    ({"stop_ceiling_s": math.nan}, "positive finite"),
    ({"health_name": "no_such_daemon", "curl_url": "http://localhost:1/healthz"}, "no port"),
    (
        {"health_name": "heartbeat", "curl_url": "http://localhost:1/api/health"},
        "serves /healthz",
    ),
    ({"health_name": "heartbeat"}, "serves /healthz"),
    ({"home_healthz": True}, "declares home_healthz"),
    ({"home_healthz": True, "curl_url": "http://localhost:1/api/health"}, "declares home_healthz"),
    ({"capabilities": _BOTH, "requires_db": True}, "declares no db_access"),
]


@pytest.mark.parametrize(("fields", "message"), _MALFORMED)
def test_a_malformed_spec_fails_where_it_is_written(
    fields: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _spec(**fields)


# ── invariants of the whole roster ──


def test_the_roster_has_standard_daemons_and_each_one_has_a_port_slot() -> None:
    specs = _healthz_specs()
    assert len(specs) >= 11
    for spec in specs.values():
        assert spec.health_name in DEFAULT_PORTS, spec.session


# meiva:note
# The standard daemons with a declared stop window: the agent-host's graceful exit drains
# running turns to their restart and the ops server drains the requests it admitted; both
# outlast root's default TERM window, so their roster entries declare `HOST_STOP_CEILING_S`
# and `OPS_STOP_CEILING_S`. Each window must be exactly its ceiling plus the margin; every
# other standard daemon declares none and renders none.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md
_DECLARED_STOP_CEILINGS = {"agent-host": HOST_STOP_CEILING_S, "ops": OPS_STOP_CEILING_S}


def test_every_standard_daemon_renders_as_exec_python_dash_m_its_module(unit_home: Path) -> None:
    """I4: the unit pid stays a direct child of root, and a declaration cannot smuggle
    undeclared environment, arguments, a stop window or sealed inputs into the rendered unit."""
    update_all_disk_images(plugin_dirs=discover_plugins(home=unit_home), home=unit_home)
    for spec in _healthz_specs().values():
        (unit,) = gen.build_units(
            [spec], capabilities=spec.capabilities, repo_root=_REPO, home=unit_home
        )
        assert unit["exec"] == [
            "/bin/sh",
            "-c",
            f"cd {_REPO} && exec {_PYTHON_M}{_module(spec)}",
        ], spec.session
        inputs = cast("list[dict[str, str]]", unit["inputs"])
        assert [entry["path"] for entry in inputs] == [str(path) for path in spec.config_inputs]
        if spec.session in _DECLARED_STOP_CEILINGS:
            ceiling = _DECLARED_STOP_CEILINGS[spec.session]
            assert spec.stop_ceiling_s == ceiling, spec.session
            assert unit["stop_timeout_s"] == ceiling + STOP_MARGIN_S, spec.session
        else:
            assert spec.stop_ceiling_s is None, spec.session
            assert "stop_timeout_s" not in unit, spec.session
        assert (unit["id"], unit["restart"], unit["attach"]) == (spec.session, "always", "root")


def test_names_are_unique_across_the_whole_roster() -> None:
    """I5: two entries sharing a session, a health name, a URL or a pidfile would fight
    over one unit, one port or one pid record, and the second would never show up."""
    specs = composition.build_services(home=meiva_home())
    sessions = [s.session for s in specs]
    assert len(set(sessions)) == len(sessions), sorted(sessions)

    healthz = [s for s in specs if s.health_name is not None]
    for key, values in {
        "health_name": [s.health_name for s in healthz],
        "curl_url": [s.curl_url for s in healthz],
        "pidfile": [_daemon_probe(s).keywords["pidfile"] for s in healthz],
        "module": [_module(s) for s in healthz],
    }.items():
        assert len(set(values)) == len(values), f"duplicate {key}: {sorted(map(str, values))}"


def test_a_healthz_endpoint_is_declared_not_left_to_its_url() -> None:
    """Every roster entry whose readiness endpoint is a `/healthz` says what kind: a
    standard daemon (`health_name`) or an endpoint that names its home (`home_healthz`).
    `meiva start` decides which ports it can check for a foreign occupant from that
    declaration, so a new `/healthz` entry cannot be silently in or out of the check."""
    for spec in composition.build_services(home=meiva_home()):
        serves_healthz = spec.curl_url is not None and spec.curl_url.endswith("/healthz")
        declared = spec.health_name is not None or spec.home_healthz
        assert serves_healthz == declared, spec.session
    assert {s.session for s in composition.build_services(home=meiva_home()) if s.home_healthz} == {
        "gate"
    }


def test_a_standard_daemons_session_derives_its_health_name() -> None:
    for session, spec in _healthz_specs().items():
        assert spec.health_name == health_name_of(session)
        assert spec.curl_url == healthz_url(health_name_of(session))


# ── the daemon side agrees with the roster ──

# The daemon-side calls that name the daemon. A mismatch is silent until boot (a name with
# no port slot) or forever (the probe reads another `name` out of /healthz and the port
# reads as taken), and a roster rename cannot see these literals.
_HEALTH_NAME_CALLS = ("start_health_server", "init_gateway_process", "install_graceful_shutdown")
_MODULE_CALLS = ("acquire_pidfile", "pidfile_holds_daemon")


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def _module_constants(tree: ast.Module) -> dict[str, str]:
    found: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            found[node.targets[0].id] = node.value.value
    return found


def _literal(node: ast.expr, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _scope_nodes(node: ast.AST) -> list[ast.AST]:
    found: list[ast.AST] = []
    for child in ast.iter_child_nodes(node):
        found.append(child)
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        found.extend(_scope_nodes(child))
    return found


def _frozen_dataclass(node: ast.ClassDef) -> bool:
    return any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "dataclass"
        and any(
            k.arg == "frozen" and isinstance(k.value, ast.Constant) and k.value.value is True
            for k in decorator.keywords
        )
        for decorator in node.decorator_list
    )


def _default_instances(tree: ast.Module) -> dict[str, str]:
    assignments = Counter(
        name for node in _scope_nodes(tree) if (name := _lexical_binding(node)) is not None
    )
    return {
        node.targets[0].id: node.value.func.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and assignments[node.targets[0].id] == 1
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and assignments[node.value.func.id] == 1
        and not node.value.args
        and not node.value.keywords
    }


def _dependency_fields(tree: ast.Module) -> dict[str, dict[str, ast.expr]]:
    classes: dict[str, dict[str, ast.expr]] = {
        node.name: {
            field.target.id: field.value
            for field in node.body
            if isinstance(field, ast.AnnAssign)
            and isinstance(field.target, ast.Name)
            and isinstance(field.value, (ast.Name, ast.Attribute))
        }
        for node in tree.body
        if isinstance(node, ast.ClassDef) and _frozen_dataclass(node)
    }
    return {name: classes[cls] for name, cls in _default_instances(tree).items() if cls in classes}


def _argument_defaults(function: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, ast.expr]:
    positional = function.args.posonlyargs + function.args.args
    return {
        argument.arg: default
        for argument, default in [
            *zip(
                positional[len(positional) - len(function.args.defaults) :],
                function.args.defaults,
                strict=True,
            ),
            *zip(function.args.kwonlyargs, function.args.kw_defaults, strict=True),
        ]
        if default is not None
    }


def _lexical_binding(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return node.id
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.alias):
        return node.asname or node.name.split(".")[0]
    if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
        return node.name
    if isinstance(node, ast.MatchMapping):
        return node.rest
    return None


def _scope_rebound(nodes: list[ast.AST]) -> set[str]:
    rebound = {name for node in nodes if (name := _lexical_binding(node)) is not None}
    rebound.update(
        node.value.id
        for node in nodes
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, (ast.Store, ast.Del))
        and isinstance(node.value, ast.Name)
    )
    return rebound


def _default_target(
    callee: ast.expr,
    defaults: dict[str, ast.expr],
    instances: dict[str, dict[str, ast.expr]],
) -> ast.expr | None:
    if isinstance(callee, ast.Name):
        return defaults.get(callee.id)
    if isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name):
        default = defaults.get(callee.value.id)
        if isinstance(default, ast.Name):
            return instances.get(default.id, {}).get(callee.attr)
    return None


def _injected_calls(tree: ast.Module) -> list[ast.Call]:
    instances = _dependency_fields(tree)
    calls: list[ast.Call] = []
    for function in tree.body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = _scope_nodes(function)
        rebound = _scope_rebound(nodes)
        defaults = {
            name: value
            for name, value in _argument_defaults(function).items()
            if name not in rebound
        }
        for node in nodes:
            if isinstance(node, ast.Call):
                target = _default_target(node.func, defaults, instances)
                if isinstance(target, (ast.Name, ast.Attribute)):
                    calls.append(ast.Call(func=target, args=node.args, keywords=node.keywords))
    return calls


def _daemon_literals(source: str, *, module: str | None = None) -> dict[str, list[str | None]]:
    tree = ast.parse(source)
    constants = _module_constants(tree)
    found: dict[str, list[str | None]] = {}
    for node in [*ast.walk(tree), *_injected_calls(tree)]:
        if not isinstance(node, ast.Call):
            continue
        callee = _call_name(node)
        if callee in _HEALTH_NAME_CALLS:
            keyword = {k.arg: k.value for k in node.keywords}
            argument = node.args[0] if node.args else keyword.get("name") or keyword.get("label")
            found.setdefault(callee, []).append(_literal(argument, constants) if argument else None)
        elif callee in _MODULE_CALLS and len(node.args) >= 2:
            found.setdefault(callee, []).append(_literal(node.args[1], constants))
    # Composition entries may delegate logging or pidfiles to their public owner.
    missing = {*_HEALTH_NAME_CALLS, *_MODULE_CALLS} - found.keys()
    if module is not None:
        for delegated in _called_function_sources(tree, module):
            for callee, values in _daemon_literals(delegated).items():
                if callee in missing:
                    found.setdefault(callee, []).extend(values)
    return found


def _rebound_names(tree: ast.Module) -> set[str]:
    rebound = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
    }
    rebound.update(node.arg for node in ast.walk(tree) if isinstance(node, ast.arg))
    rebound.update(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    return rebound


def _import_bindings(tree: ast.Module, module: str) -> list[tuple[str, str, str | None]]:
    bindings: list[tuple[str, str, str | None]] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            target = (
                importlib.util.resolve_name(
                    "." * node.level + node.module, module.rpartition(".")[0]
                )
                if node.level
                else node.module
            )
            bindings.extend(
                (alias.asname or alias.name, target, alias.name) for alias in node.names
            )
        elif isinstance(node, ast.Import):
            bindings.extend(
                (alias.asname or alias.name, alias.name, None)
                for alias in node.names
                if alias.asname or "." not in alias.name
            )
    return bindings


def _calls_for_import(
    calls: list[ast.expr], *, bound: str, target: str, member: str | None
) -> list[tuple[str, str]]:
    found = (
        [(target, member) for call in calls if isinstance(call, ast.Name) and call.id == bound]
        if member is not None
        else []
    )
    owner = target + "." + member if member is not None else target
    found.extend(
        (owner, call.attr)
        for call in calls
        if isinstance(call, ast.Attribute)
        and isinstance(call.value, ast.Name)
        and call.value.id == bound
    )
    return found


def _called_imports(tree: ast.Module, module: str) -> list[tuple[str, str]]:
    calls = [
        node.func
        for node in [*ast.walk(tree), *_injected_calls(tree)]
        if isinstance(node, ast.Call)
    ]
    rebound = _rebound_names(tree)
    imports = Counter(
        alias.asname or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    )
    found: list[tuple[str, str]] = []
    for bound, target, member in _import_bindings(tree, module):
        if bound not in rebound and imports[bound] == 1:
            found.extend(_calls_for_import(calls, bound=bound, target=target, member=member))
    return found


def _called_function_sources(tree: ast.Module, module: str) -> list[str]:
    found: list[str] = []
    for target, name in _called_imports(tree, module):
        path = _REPO / (target.replace(".", "/") + ".py")
        if not path.exists():
            path = _REPO / target.replace(".", "/") / "__init__.py"
        if not path.exists():
            continue
        delegated = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_constants(delegated)
        prefix = "\n".join(f"{key} = {value!r}" for key, value in constants.items())
        for function in delegated.body:
            if (
                isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                and function.name == name
            ):
                found.append(prefix + "\n" + ast.unparse(function))
    return found


def test_every_standard_daemon_names_itself_as_the_roster_does() -> None:
    """The names the daemon passes to its health server, log init, shutdown label and
    pidfile guard are the roster's derived names, and the module it guards is the one
    `python -m` runs."""
    for spec in _healthz_specs().values():
        module = _module(spec)
        source = (_REPO / (module.replace(".", "/") + ".py")).read_text(encoding="utf-8")
        found = _daemon_literals(source, module=module)
        for callee in _HEALTH_NAME_CALLS:
            assert found.get(callee), f"{module}: no {callee}(...) with a literal name"
            assert set(found[callee]) == {spec.health_name}, (module, callee, found[callee])
        for callee in _MODULE_CALLS:
            assert found.get(callee), f"{module}: no {callee}(path, module) with a literal"
            assert set(found[callee]) == {module}, (module, callee, found[callee])


def test_the_literal_finder_sees_a_mismatch() -> None:
    """The check above is only worth having if it can fail."""
    source = (
        "_M = 'pkg.daemon'\n"
        "async def run():\n"
        "    await start_health_server('beat')\n"
        "def main():\n"
        "    init_gateway_process(name='beat-x')\n"
        "    install_graceful_shutdown('b')\n"
        "    acquire_pidfile(P, _M)\n"
    )
    assert _daemon_literals(source) == {
        "start_health_server": ["beat"],
        "init_gateway_process": ["beat-x"],
        "install_graceful_shutdown": ["b"],
        "acquire_pidfile": ["pkg.daemon"],
    }


def test_the_literal_finder_follows_each_function_default_in_its_own_scope() -> None:
    source = (
        "def boot(start=start_health_server):\n"
        "    start('wrong-health-name')\n"
        "def unrelated(start=other):\n"
        "    start('not-a-health-name')\n"
    )
    assert _daemon_literals(source) == {"start_health_server": ["wrong-health-name"]}


def test_the_literal_finder_follows_the_default_frozen_dependency_instance() -> None:
    source = (
        "@dataclass(frozen=True)\n"
        "class Dependencies:\n"
        "    log: Callable = init_gateway_process\n"
        "    stop: Callable = install_graceful_shutdown\n"
        "DEFAULT = Dependencies()\n"
        "def boot(deps=DEFAULT):\n"
        "    deps.log(name='wrong-log-name')\n"
        "    deps.stop('wrong-stop-name')\n"
        "def unrelated(deps):\n"
        "    deps.log(name='not-a-log-name')\n"
    )
    assert _daemon_literals(source) == {
        "init_gateway_process": ["wrong-log-name"],
        "install_graceful_shutdown": ["wrong-stop-name"],
    }


@pytest.mark.parametrize(
    "change",
    [
        "override",
        "mutable",
        "rebound",
        "changed-field",
        "imported-default",
        "defined-default",
        "imported-class",
    ],
)
def test_the_literal_finder_refuses_an_unproven_dependency_instance(change: str) -> None:
    source = (
        "@dataclass(frozen=True)\n"
        "class Dependencies:\n"
        "    log: Callable = init_gateway_process\n"
        "DEFAULT = Dependencies()\n"
        "def boot(deps=DEFAULT):\n"
        "    deps.log(name='unknown')\n"
    )
    source = {
        "override": source.replace("Dependencies()", "Dependencies(log=other)"),
        "mutable": source.replace("frozen=True", "frozen=False"),
        "rebound": source.replace("    deps.log(", "    deps = other\n    deps.log("),
        "changed-field": source.replace("    deps.log(", "    deps.log = other\n    deps.log("),
        "imported-default": source.replace("def boot", "from other import DEFAULT\ndef boot"),
        "defined-default": source.replace("def boot", "def DEFAULT():\n    pass\ndef boot"),
        "imported-class": source.replace("DEFAULT =", "from other import Dependencies\nDEFAULT ="),
    }[change]
    assert _daemon_literals(source) == {}


@pytest.mark.parametrize(
    ("module", "call", "health_name"),
    [
        ("services.agent_runner.agent_ops.daemon", 'install_graceful_shutdown("ops")', "ops"),
        (
            "services.agent_runner.page_server.daemon",
            'install_graceful_shutdown("page_server")',
            "page_server",
        ),
    ],
)
def test_the_literal_finder_exposes_a_real_daemon_shutdown_name_mismatch(
    module: str,
    call: str,
    health_name: str,
) -> None:
    source = (_REPO / (module.replace(".", "/") + ".py")).read_text()
    assert call in source
    changed = source.replace(call, call.replace(health_name, "wrong-name"))
    assert _daemon_literals(changed, module=module)["install_graceful_shutdown"] == ["wrong-name"]


@pytest.mark.parametrize(
    "body",
    [
        "start = other\n    start('unknown')",
        "def nested(start):\n        start('unknown')",
        "from other import start\n    start('unknown')",
        "import other as start\n    start('unknown')",
        "def start(name):\n        pass\n    start('unknown')",
        "class start:\n        pass\n    start('unknown')",
        "try:\n        pass\n    except Error as start:\n        start('unknown')",
        "match value:\n        case start:\n            start('unknown')",
    ],
)
def test_the_literal_finder_does_not_follow_a_rebound_or_nested_default(body: str) -> None:
    assert _daemon_literals(f"def boot(start=start_health_server):\n    {body}\n") == {}


def test_the_literal_finder_follows_an_injected_public_pidfile_owner() -> None:
    source = (
        "from services.agent_runner.agent_ops import boot\n"
        "def run(is_running=boot.is_running, write_pidfile=boot.write_pidfile):\n"
        "    is_running(config, runtime=runtime)\n"
        "    write_pidfile(config, runtime=runtime)\n"
    )
    assert _daemon_literals(source, module="services.agent_runner.agent_ops.daemon") == {
        "pidfile_holds_daemon": ["services.agent_runner.agent_ops.daemon"],
        "acquire_pidfile": ["services.agent_runner.agent_ops.daemon"],
    }


def test_the_literal_finder_follows_the_public_pidfile_owner() -> None:
    source = (
        "from services.agent_runner.page_server import resources as owned\n"
        "def main():\n"
        "    owned.write_pidfile(config, runtime=runtime)\n"
        "    owned.is_running(config, runtime=runtime)\n"
    )
    found = _daemon_literals(source, module="services.agent_runner.page_server.daemon")
    assert found == {
        "acquire_pidfile": ["services.agent_runner.page_server.daemon"],
        "pidfile_holds_daemon": ["services.agent_runner.page_server.daemon"],
    }
    delegated = "\n".join(
        _called_function_sources(ast.parse(source), "services.agent_runner.page_server.daemon")
    )
    wrong_name = delegated.replace("services.agent_runner.page_server.daemon", "wrong.daemon")
    assert _daemon_literals(wrong_name) == {
        "acquire_pidfile": ["wrong.daemon"],
        "pidfile_holds_daemon": ["wrong.daemon"],
    }


@pytest.mark.parametrize(
    "source",
    [
        "from services.agent_runner.page_server import resources as owned\n"
        "owned = other\nowned.write_pidfile(config, runtime=runtime)\n",
        "from services.agent_runner.page_server import resources as owned\n"
        "def main(owned):\n    owned.write_pidfile(config, runtime=runtime)\n",
        "from services.agent_runner.page_server import resources as owned\n"
        "def main():\n    owned = other\n    owned.write_pidfile(config, runtime=runtime)\n",
        "from services.agent_runner.page_server import resources as owned\n"
        "other.write_pidfile(config, runtime=runtime)\n",
        "from services.agent_runner.page_server import resources as owned\n"
        "from other import resources as owned\nowned.write_pidfile(config, runtime=runtime)\n",
    ],
)
def test_the_literal_finder_does_not_attribute_an_unknown_resource_owner(source: str) -> None:
    assert _called_imports(ast.parse(source), "services.agent_runner.page_server.daemon") == []
    assert _daemon_literals(source, module="services.agent_runner.page_server.daemon") == {}


def test_the_public_probe_builder_is_still_the_roster_door() -> None:
    """Plugin services import `daemon_identity` from `services.supervision.roster`; it must stay there."""
    assert roster.daemon_identity is daemon_identity
    assert roster.healthz_daemon is healthz_daemon
