# meiva:note
# The SDK entry completes its own namespace imports before allowing lazy plugin loading.
# A bare non-agent import leaves context unbound and does not import database, Redis,
# HTTP or model connection stacks. Process clients retain their supplied configuration
# and build connections only on first use; installation clients outlive an attachment,
# while bound calls retain the attachment's own clients. A reload must preserve the
# first-loaded code image and process-context slots instead of relabeling or clearing
# already-running code.
#
# Booting an external process is the same work whoever borrowed the identity: read
# the launch environment, resolve the machine, build the database factory, and
# (only when nothing is bound yet) own a client set. The clock comes from
# here too because an independent attachment has no process clock to inherit. The
# takeover and the harness differ in *what proves the identity*; they must not
# differ in how the process around it is built, or a fix to one boot path silently
# misses the other. The plugin-state codec lives here for the same reason: the
# agent layer owns it, both attachments replay through it, and one load site keeps
# the upward edge counted once.
# meiva:link meiva/docs/boot-contracts.meiva.okf.md
# meiva:link base/agents/context/external_attachment/docs/external-attachment.meiva.okf.md

from __future__ import annotations

import atexit
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, assert_never

from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet, DatabaseFactory
from base.agents.context.gateway_client.transport import GatewayTransportInputs
from base.agents.context.identity import AgentIdentity
from base.agents.messages.external_caller import external_caller
from base.agents.sdk import call_policy
from base.agents.sdk.call_policy import SamplingPolicyOwner
from base.api_contracts.agent_contract import Harness
from base.api_contracts.caller_identity import CallerIdentity
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.host.env.agent_env import AGENT_ID_ENV
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.plugins.loading import StateGate
from meiva.mcps import McpClients
from meiva.sdk_surface import meiva_module, process_context
from meiva.sdk_surface.process_context import ContextOutsideProcessError
from meiva.sdk_surface.pure.load_plan import (
    IdentityFacts,
    LaunchFacts,
    LoadFacts,
    LoadStep,
    MissingNameFacts,
    MissingNameStep,
    launched_process,
    plan_load,
    plan_missing_name,
)


def _redis(url: str) -> object:
    import redis as redis_lib

    from base.events.live.redis_client import RESILIENCE_KWARGS

    if not url:
        raise RuntimeError(
            "MEIVA_REDIS_URL not set — Redis ops should not be called in container mode"
        )
    client_class: Any = redis_lib.Redis
    return client_class.from_url(
        url,
        decode_responses=True,
        **{**RESILIENCE_KWARGS, "socket_timeout": 10.0},
    )


# meiva:note
# The gateway endpoint of `config`, resolved when the first client asks for it: a root builds its
# clients before the configuration has to be readable.
def gateway_endpoint(config: ConfigBoot) -> Callable[[], str]:
    def endpoint() -> str:
        from base.cluster.machine import gateway_api_base

        return gateway_api_base(config.view.gateway.gateway_url)

    return endpoint


def _gateway(url: str, *, config: ConfigBoot) -> Any:
    import httpx

    from base.cluster.machine import gateway_bearer
    from base.cluster.machine.boot import gateway_access_from_boot
    from base.host.net.auth import bearer_header
    from base.host.net.http_dial import transport_for_url

    bearer = gateway_bearer(gateway_access_from_boot(config))
    return httpx.Client(
        base_url=url,
        timeout=httpx.Timeout(config.view.gateway.gateway_client_http_timeout_seconds),
        headers=bearer_header(bearer),
        transport=transport_for_url(url),
    )


# meiva:note
# The sampling owner of a process with no service lifespan to schedule its cleanup (a launched
# child, a script, a command-line inventory). It closes at interpreter exit: finite cleanup, not a
# guarantee of a nonzero exit or of delivery after a hard exit. `uninstall` unregisters the close of
# the installation that retains the owner, so a removed surface leaves no callback behind.
def process_sampling_owner() -> SamplingPolicyOwner:
    owner = call_policy.process_sampling_owner()
    atexit.register(owner.close)
    return owner


def database_factory(*, config: ConfigBoot, machine: MachineIdentityReader) -> DatabaseFactory:
    from threading import Lock

    from base.native_process.code_version import CodeVersion

    version = CodeVersion(process_context.loaded_code_image())
    lock = Lock()

    gate: Any = None

    def database() -> Any:
        nonlocal gate
        from base.cluster.dataplane.direct_url import ClusterDirectUrl
        from base.db import Database
        from base.db.code_version_gate import ProcessDbGate
        from base.db.config import db_config_from_boot

        if not config.view.data_plane.db_url:
            raise RuntimeError(
                "MEIVA_DB_URL not set — SQL ops should not be called in container mode"
            )
        with lock:
            if gate is None:
                gate = ProcessDbGate(version=version.get, process="unknown")
        return Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host),
        )

    return database


# meiva:note
# The clients one SDK process owner builds from its prepared configuration. Nothing connects at
# construction: every connection, the event pipeline and the MCP client set build on first use,
# reading `config` then. The root names every owner the clients share: the `machine` identity it
# also gives its logger and event pipeline, the `database` factory it holds and the `gateway_url`
# (a string or a resolver called on first use). `clients_over` derives the endpoint and
# `derived_clients` also the database, from the configuration and the identity.
def process_clients(
    *,
    config: ConfigBoot,
    machine: MachineIdentityReader,
    database: DatabaseFactory,
    gateway_url: str | Callable[[], str],
) -> ClientSet:
    def pipeline() -> Any:
        from base.paths.home import resolve_meiva_home
        from base.telemetry import build_pipeline

        return build_pipeline(database=database, machine=machine, home=resolve_meiva_home())

    # meiva:note
    # The MCP client set reads its request timeout once, when the first MCP call builds it.
    def mcp_clients() -> McpClients:
        from base.paths import mcp_daemon_shared_socket
        from base.paths.home import resolve_meiva_home

        return McpClients(
            Path(mcp_daemon_shared_socket(resolve_meiva_home())),
            config.view.sandbox.mcp_connect_timeout_seconds,
        )

    return ClientSet(
        gateway_url=gateway_url,
        database=database,
        pipeline_factory=pipeline,
        redis=lambda: _redis(config.view.data_plane.redis_url),
        gateway=lambda url: _gateway(url, config=config),
        factories={
            McpClients: mcp_clients,
            GatewayTransportInputs: lambda: GatewayTransportInputs(
                max_retries_reader=lambda: config.view.gateway.gateway_client_max_retries,
                retry_delay_reader=lambda: config.view.gateway.gateway_client_retry_delay_seconds,
                memory_deadline_reader=lambda: config.view.services.memory_search_deadline_seconds,
            ),
        },
    )


# meiva:note
# The clients of a root that holds its own database factory: the gateway endpoint derives from the
# configuration.
def clients_over(
    config: ConfigBoot, machine: MachineIdentityReader, database: DatabaseFactory
) -> ClientSet:
    return process_clients(
        config=config, machine=machine, database=database, gateway_url=gateway_endpoint(config)
    )


# meiva:note
# The clients of a root that owns nothing but its configuration and its machine identity: the
# database factory and the gateway endpoint derive from them.
def derived_clients(config: ConfigBoot, machine: MachineIdentityReader) -> ClientSet:
    return clients_over(config, machine, database_factory(config=config, machine=machine))


def context_from_description(
    description: dict[str, Any],
    *,
    config: ConfigBoot,
    original_incarnation: RuntimeIncarnation | None = None,
) -> MeivaContext:
    machine = ConfiguredMachineIdentity(config)
    return MeivaContext.from_description(
        description,
        clients=process_clients(
            config=config,
            machine=machine,
            database=database_factory(config=config, machine=machine),
            gateway_url=description["gateway_url"],
        ),
        machine=machine,
        original_incarnation=original_incarnation,
    )


def launch_agent_id(env: Mapping[str, str]) -> int | None:
    raw = env.get(AGENT_ID_ENV)
    return None if raw is None else int(raw)


def launched_context(*, env: Mapping[str, str], config: ConfigBoot) -> MeivaContext | None:
    agent_id = launch_agent_id(env)
    if agent_id is None:
        return None
    if not config.prepared:
        config.read_process_environment()
    machine = ConfiguredMachineIdentity(config)
    raw_harness = env.get("MEIVA_HARNESS", "").strip()
    harness = Harness(raw_harness) if raw_harness in set(Harness) else Harness.MEIVA
    return MeivaContext(
        identity=AgentIdentity(agent_id=agent_id, owns_loop=False),
        harness=harness,
        clients=derived_clients(config, machine),
        machine=machine,
        clock_factory=lambda: Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
    )


def _door_import_complete() -> bool:
    return bool(meiva_module().__dict__.get("_init_complete", False))


def _identity_facts(context: MeivaContext | None) -> IdentityFacts | None:
    identity = None if context is None else context.identity
    if identity is None:
        return None
    return IdentityFacts(
        has_agent_id=identity.agent_id is not None, owns_loop=bool(identity.owns_loop)
    )


def _launch_facts(context: MeivaContext | None, env: Mapping[str, str]) -> LaunchFacts:
    return LaunchFacts(
        host=process_context.is_host_process(),
        context_bound=context is not None,
        identity=_identity_facts(context),
        env_has_agent_id=AGENT_ID_ENV in env,
    )


@dataclass(frozen=True, slots=True)
class _SlotFacts:
    installed: bool
    faces: bool
    attempted: bool
    failed: bool


def _slot_facts() -> _SlotFacts:
    from meiva.sdk_surface.installation import Loading, installed, slot

    current = installed()
    holder = slot()
    return _SlotFacts(
        installed=current is not None,
        faces=current is not None and current.faces,
        attempted=isinstance(holder, Loading),
        failed=isinstance(holder, Loading) and holder.failure is not None,
    )


# meiva:note
# Idempotently load the plugin surface of this process; the facade `meiva.ensure_plugins_loaded`
# forwards here. This is the place that reads the process: the installation slot, the bound
# context and the launch environment become `LoadFacts`, `plan_load` chooses the step, and only a
# plan that loads a surface resolves the owners it needs (`LoadInputs`) before `load_surface`
# installs them. Every other step is settled by `settle_installed` from the step and the context
# alone. A `clients` set the caller supplies is the one the installation meters through; none
# supplied means the installation builds its own cold set and closes it at interpreter exit,
# so an attachment's shorter-lived clients are never borrowed.
def ensure_plugins_loaded(
    *,
    surface: bool = True,
    config: ConfigBoot | None = None,
    machine: MachineIdentityReader | None = None,
    context: MeivaContext | None = None,
    clients: ClientSet | None = None,
    validate_state: StateGate | None = None,
) -> None:
    from meiva.sdk_surface import install as _sdk_install

    if context is not None:
        process_context.bind_context(context)
    else:
        context = process_context.bound_context()
    env = os.environ
    state = _slot_facts()
    plan = plan_load(
        LoadFacts(
            surface=surface,
            installed=state.installed,
            faces=state.faces,
            attempted=state.attempted,
            failed=state.failed,
            config_supplied=config is not None,
            context_bound=context is not None,
            launched=launched_process(_launch_facts(context, env)),
        )
    )
    if plan.step is not LoadStep.LOAD_SURFACE:
        settle_installed(plan.step, context)
        return
    if plan.prepare_launched:
        context, config = _prepare_launched_context(env)
    disabled = process_disable_entries()
    _apply_disable(disabled)
    _sdk_install.mark_load_attempt()
    try:
        inputs = _load_inputs(
            surface=surface,
            disabled=disabled,
            config=config,
            machine=machine,
            context=context,
            clients=clients,
            caller=external_caller(env=env),
            validate_state=validate_state,
        )
    except BaseException as exc:
        _sdk_install.mark_load_failed(exc)
        raise
    load_surface(inputs)
    _bind_installed_catalog(inputs.context)


# meiva:note
# The steps of a load plan that need no new owner: they act on the installation already in the
# slot and on the context the caller holds. `LOAD_SURFACE` is not one of them: it needs the owners
# `LoadInputs` carries and goes through `load_surface`, so asking for it here is a caller bug.
def settle_installed(step: LoadStep, context: MeivaContext | None) -> None:
    from meiva.sdk_surface import install as _sdk_install

    match step:
        case LoadStep.RAISE_FAILURE:
            _sdk_install.raise_load_failure()
        case LoadStep.BIND_CATALOG:
            _bind_installed_catalog(context)
        case LoadStep.UPGRADE_FACES:
            _sdk_install.mark_load_attempt()
            upgrade_faces()
            _bind_installed_catalog(context)
        case LoadStep.NOTHING:
            return
        case LoadStep.LOAD_SURFACE:
            raise ValueError("a surface load needs its owners: resolve LoadInputs and load_surface")
        case _:
            assert_never(step)


# meiva:note
# The SDK-disable entries this process's environment names. A root that loads plugins itself
# (the agent host) asks here so the surface it installs honors the same `MEIVA_SDK_DISABLE`
# a launched child does.
def process_disable_entries() -> tuple[str, ...]:
    from meiva.sdk_surface import sdk_disable

    return tuple(sdk_disable.env_entries(os.environ))


# meiva:note
# Everything one surface load reads, resolved by its caller. `disabled` are the SDK-disable
# entries applied before any plugin. `config`, `machine` and `clients` are the owners the
# installation retains, `clock` is the root clock it serves timestamped operations with, `home`
# the MEIVA_HOME its authority reads `.env` from and `catalog` the model facts it hands to
# model consumers. `sampling` is the owner of SDK-call sampling the installation retains and
# `caller` the external tool profile the process opted into, parsed once by the facade.
@dataclass(frozen=True, slots=True)
class LoadInputs:
    surface: bool
    disabled: tuple[str, ...]
    config: ConfigBoot
    machine: MachineIdentityReader
    clients: ClientSet
    sampling: SamplingPolicyOwner
    caller: CallerIdentity | None
    clock: Clock
    home: Path
    catalog: ModelCatalog
    provider_keys: ProviderKeys
    context: MeivaContext | None
    validate_state: StateGate | None


def _load_inputs(
    *,
    surface: bool,
    disabled: tuple[str, ...],
    config: ConfigBoot | None,
    machine: MachineIdentityReader | None,
    context: MeivaContext | None,
    clients: ClientSet | None,
    caller: CallerIdentity | None,
    validate_state: StateGate | None,
) -> LoadInputs:
    from base import paths
    from base.lm.factory import process_provider_keys
    from base.packages.plugin_providers import build_model_catalog

    if config is None:
        config = ConfigBoot()
        config.read_process_environment()
    if machine is None:
        machine = _context_machine(context) or ConfiguredMachineIdentity(config)
    if clients is None:
        clients = derived_clients(config, machine)
        atexit.register(clients.close)
    if context is not None and context.clock_factory is not None:
        clock = context.require_clock()
    else:
        clock = Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS)
    catalog = build_model_catalog()
    sampling = process_sampling_owner()
    return LoadInputs(
        surface=surface,
        disabled=disabled,
        config=config,
        machine=machine,
        clients=clients,
        sampling=sampling,
        caller=caller,
        clock=clock,
        home=paths.meiva_home(),
        catalog=catalog,
        provider_keys=process_provider_keys(catalog),
        context=context,
        validate_state=validate_state,
    )


def _context_machine(context: MeivaContext | None) -> MachineIdentityReader | None:
    return None if context is None else context.machine


def _apply_disable(entries: tuple[str, ...]) -> None:
    from meiva.sdk_surface import sdk_disable

    sdk_disable.apply_entries(list(entries))


def upgrade_faces() -> None:
    from base.packages.plugins.loading.faces import load_agent_faces
    from meiva.sdk_surface import install as _sdk_install

    try:
        load_agent_faces()
        _sdk_install.mark_faces_loaded()
    except BaseException as exc:
        _sdk_install.mark_load_failed(exc)
        raise


def load_surface(inputs: LoadInputs) -> None:
    from base.config.service_read import ConfigAuthority
    from meiva.sdk_surface import install as _sdk_install
    from meiva.sdk_surface.extensions import load_extensions

    try:
        authority = ConfigAuthority.deferred(
            runtime=inputs.config.view,
            build_all_domains=inputs.config.complete_read_model,
            env_path=inputs.home / ".env",
        )
        load_extensions(
            surface=inputs.surface,
            catalog=inputs.catalog,
            provider_keys=inputs.provider_keys,
            authority=authority,
            clients=inputs.clients,
            sampling=inputs.sampling,
            clock=inputs.clock,
            external_caller=inputs.caller,
            disabled_entries=inputs.disabled,
            validate_state=inputs.validate_state,
        )
        if not inputs.surface:
            _sdk_install.mark_faces_loaded()
    except BaseException as exc:
        _sdk_install.mark_load_failed(exc)
        raise


def initialize_context(
    context: MeivaContext,
    *,
    surface: bool = True,
    config: ConfigBoot | None,
    clients: ClientSet | None,
    validate_state: StateGate | None = None,
) -> None:
    ensure_plugins_loaded(
        context=context,
        surface=surface,
        config=config,
        machine=context.machine,
        clients=clients,
        validate_state=validate_state,
    )


def _bind_installed_catalog(context: MeivaContext | None) -> None:
    if context is None:
        return
    from meiva.sdk_surface import settings as _settings
    from meiva.sdk_surface.installation import installed

    if installed() is not None:
        catalog = _settings.model_catalog()
        if context.catalog is not catalog:
            process_context.bind_context(replace(context, catalog=catalog))


def _prepare_launched_context(env: Mapping[str, str]) -> tuple[MeivaContext, ConfigBoot]:
    config = ConfigBoot()
    context = launched_context(env=env, config=config)
    if context is None:
        raise RuntimeError("launched process identity disappeared during SDK initialization")

    atexit.register(context.clients.close)
    process_context.bind_context(context)
    return context, config


def is_launched_process() -> bool:
    return launched_process(_launch_facts(process_context.bound_context(), os.environ))


def maybe_load_plugins_for_missing(name: str) -> bool:
    from meiva.sdk_surface import agent_identity
    from meiva.sdk_surface import install as _sdk_install

    def facts(*, launched_child: bool | None) -> MissingNameFacts:
        state = _slot_facts()
        return MissingNameFacts(
            private=name.startswith("_"),
            entry_complete=_door_import_complete(),
            failed=state.failed,
            installed=state.installed,
            attempted=state.attempted,
            launched_child=launched_child,
        )

    step = plan_missing_name(facts(launched_child=None))
    if step is MissingNameStep.ASK_LAUNCHED:
        step = plan_missing_name(facts(launched_child=agent_identity.is_launched_child()))
    match step:
        case MissingNameStep.RAISE_FAILURE:
            _sdk_install.raise_load_failure()
            return False
        case MissingNameStep.LOAD:
            ensure_plugins_loaded()
            return _slot_facts().installed
        case MissingNameStep.LOADED:
            return True
        case MissingNameStep.IGNORE:
            return False
        case MissingNameStep.ASK_LAUNCHED:
            raise RuntimeError("the missing-name plan asked whether the process was launched twice")
        case _:
            assert_never(step)


def bind_launched_context() -> MeivaContext | None:
    env = os.environ
    if launch_agent_id(env) is None:
        return None
    config = ConfigBoot()
    launched = launched_context(env=env, config=config)
    if launched is None:
        return None
    process_context.bind_context(launched)
    atexit.register(launched.clients.close)
    return launched


# meiva:note
# A provider harness runs the agent loop itself and exports a registration handle
# instead of MEIVA_AGENT_ID, so every "is this an agent-launched child" test above
# answers no. The attachment is made on the first read that needs an identity —
# `meiva.context`, `meiva.self.AGENT_ID`, `meiva.attach`, a plugin namespace such as
# `meiva.tasks` — and never at import: attaching replays the agent's plugin-state
# snapshot through the codec, which opens the database, and a provider runs a fresh
# Python per tool call.
#
# Nothing caches the attempt here. A session that ended must not be retried on every
# attribute read, and the SDK already owns that answer: a failed attach is recorded
# in the install slot (`mark_load_failed`), whose readers raise it instead of
# loading again. Two concurrent first reads cannot build two attachments either —
# `ExternalAttachment.open` takes a process-wide non-blocking lock.
#
# `HarnessAttachError` is a `ContextOutsideProcessError` so the readers that
# tolerate a missing context (`meiva.files` resolving a relative path,
# `meiva.shell` choosing a working directory) keep their own fallback, while the
# reads that need an identity report this reason. A cell that failed half-way
# leaves a plugin surface with no identity behind it, so the failure drops it.
# meiva:link meiva/self/harness/docs/harness-attach.meiva.okf.md
_HARNESS_HANDLE_ENV = "MEIVA_HARNESS_HANDLE"


class HarnessAttachError(ContextOutsideProcessError):
    pass


def harness_failure(exc: BaseException) -> HarnessAttachError:
    return HarnessAttachError(
        "this process was started by a harness bridge, but the SDK could not borrow the "
        f"session's identity: {exc}. Calls that do not need the identity still work (an "
        "absolute-path file read, meiva.ui); meiva.context, meiva.self.AGENT_ID, the "
        "plugin namespaces (meiva.tasks) and meiva.agents all fail with this error. A "
        "session that expired or ended cannot be attached again: stop retrying and report "
        "this failure in your reply instead of working around it."
    )


def record_harness_failure(exc: BaseException) -> None:
    from meiva.sdk_surface import install as _sdk_install

    failure = harness_failure(exc)
    try:
        _sdk_install.uninstall()
    except Exception:
        from base.log import logger

        logger.opt(exception=True).warning("rolling back the failed harness attachment failed")
    _sdk_install.mark_load_failed(failure)
    from base.log import logger

    logger.warning(f"harness identity attachment failed: {exc}")


# meiva:note
# What an external process needs before it can act as an agent, when it borrows a bound context:
# the machine and the database factory of that context. The configuration, the clients and the
# clock stay the context's own, so this boot holds none of them.
@dataclass(frozen=True, slots=True)
class BorrowedBoot:
    machine: MachineIdentityReader
    database: DatabaseFactory


# meiva:note
# What an external process needs before it can act as an agent, when nothing is bound yet and it
# owns its roots: the prepared configuration, the machine, the database factory the clients share,
# the clients themselves and the clock an independent attachment has no process clock to inherit.
@dataclass(frozen=True, slots=True)
class OwnedBoot:
    config: ConfigBoot
    machine: ConfiguredMachineIdentity
    database: DatabaseFactory
    clients: ClientSet
    clock: Clock

    # meiva:note
    # The context an independent attachment binds before it has any: the owned clients, the clock
    # and the machine of this boot.
    def new_context(self) -> MeivaContext:
        clock = self.clock
        return MeivaContext(clients=self.clients, clock_factory=lambda: clock, machine=self.machine)


type ExternalBoot = BorrowedBoot | OwnedBoot


def borrow_external_process(bound: MeivaContext) -> BorrowedBoot:
    return BorrowedBoot(machine=bound.require_machine(), database=bound.clients.database)


def own_external_process(config: ConfigBoot) -> OwnedBoot:
    if not config.prepared:
        config.read_process_environment()
    machine = ConfiguredMachineIdentity(config)
    database = database_factory(config=config, machine=machine)
    return OwnedBoot(
        config=config,
        machine=machine,
        database=database,
        clients=clients_over(config, machine, database),
        clock=Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
    )


# meiva:note
# Bring up the plugin surface of an external process around its attachment: an owned boot hands
# the load its configuration and clients, a borrowed one leaves both to the context it borrows.
def initialize_external(
    context: MeivaContext, boot: ExternalBoot, *, validate_state: StateGate | None
) -> None:
    match boot:
        case OwnedBoot():
            initialize_context(
                context,
                surface=False,
                config=boot.config,
                clients=boot.clients,
                validate_state=validate_state,
            )
        case BorrowedBoot():
            initialize_context(
                context,
                surface=False,
                config=None,
                clients=None,
                validate_state=validate_state,
            )
        case _:
            assert_never(boot)


def plugin_state_codec() -> Any:
    from base.agents.context.external_attachment import plugin_state_codec as _codec

    return _codec
