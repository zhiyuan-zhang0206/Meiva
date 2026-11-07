import asyncio
import logging
import secrets
from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext
from functools import partial
from pathlib import Path
from typing import Literal

import psutil
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import TypeAdapter
from starlette.exceptions import HTTPException as StarletteHTTPException

from base import paths, telemetry
from base.api_contracts.agent_contract import MeivaAgentError
from base.clock import SYSTEM_TIMERS, Clock
from base.cluster.machine.boot import gateway_access_from_boot
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.rate_limit import LoginRateLimitConfig, LoginRateLimiter
from base.config import ConfigBoot, Settings
from base.config.service_read import ConfigAuthority
from base.daemon.endpoints import ServiceEndpoints
from base.events.live.bus import EventBus, event_bus_config_from_config
from base.host.db_authority.api import AcceptanceCache
from base.host.system.cron.health_job import register_os_cron
from base.host.system.hostport import Host
from base.host.system.job_values import JobRoots
from base.lm.catalog import ModelCatalog
from base.packages.composer_commands import CommandCatalog, load_skill_names
from base.packages.plugin_providers import build_model_catalog
from base.sessions.backend import get_shell_backend
from gateway.agents import launch_retry as agents_launch_retry_router
from gateway.agents import lifecycle as agents_lifecycle_router
from gateway.agents import router as agents_router
from gateway.agents import state as agents_state_router
from gateway.agents.history import conversation as conversation_router
from gateway.agents.history import timeline as timeline_router
from gateway.agents.history import understanding as agents_understanding_router
from gateway.agents.notice_operations import notices as notices_router
from gateway.agents.notice_operations import router as guarded_notices_router
from gateway.agents.reply_suggestion import feedback as suggestion_feedback_router
from gateway.agents.reply_suggestion import router as reply_suggestion_router
from gateway.agents.reply_suggestion.configuration import reply_suggestion_policy
from gateway.agents.reply_suggestion.service import ReplySuggestionService
from gateway.agents.task_assignment import router as task_assignments_router
from gateway.agents.to_user import document as to_user_router
from gateway.agents.transfer import router as agents_transfer_router
from gateway.agents.transfer.bundle_store import TransferBundleStore
from gateway.agents.user_delivery import asks as asks_router
from gateway.agents.user_delivery import router as user_delivery_router
from gateway.alerts import router as alerts_router
from gateway.cluster import bootstrap as bootstrap_router
from gateway.cluster import machine_pause as machine_pause_router
from gateway.cluster import router as cluster_router
from gateway.cluster import status as status_router
from gateway.cluster.harness_probe import HarnessProbeCache, ensure_machine_harnesses
from gateway.cluster.monitor import alert_classes as alert_classes_router
from gateway.cluster.monitor import ops_monitor as ops_monitor_router
from gateway.cluster.process_boot import LOADED_IMAGE
from gateway.cluster.roster.roster_probe import IdentityMismatchLog
from gateway.cluster.server import main as _run_gateway
from gateway.cluster.status import StatusCache
from gateway.events import agent_events as agent_events_router
from gateway.events import computer_traces as computer_traces_router
from gateway.events import merged as merged_stream_router
from gateway.events import metrics as metrics_router
from gateway.events import notification_outbox
from gateway.events import resolutions as event_resolutions_router
from gateway.events import router as events_router
from gateway.events import system as system_router
from gateway.extensions import inventory as inventory_router
from gateway.extensions import packages as packages_router
from gateway.extensions import plugin_ui as plugin_ui_router
from gateway.extensions import plugins as plugins_router
from gateway.extensions import registry as extensions_registry_router
from gateway.extensions import skills as skills_router
from gateway.extensions import ui_contributions as ui_contributions_router
from gateway.http.auth import rejection_log
from gateway.http.auth import router as auth_router
from gateway.http.auth.cluster_middleware import cluster_auth_middleware
from gateway.http.auth.login_password_store import LoginPasswords
from gateway.http.auth.request_principal import SessionKeys
from gateway.http.auth.session_store import SessionStore, SessionTouchThrottle
from gateway.http.config import (
    GatewayConfig,
    app_state_as,
    gateway_config_from_boot,
    placeholder_config,
)
from gateway.http.error_handlers import (
    http_exception_handler,
    meiva_agent_error_handler,
    model_catalog_unavailable_handler,
    request_validation_error_handler,
    unhandled_exception_handler,
)
from gateway.http.middleware import latency, runtime_metrics
from gateway.http.middleware.error_envelope import request_trace_middleware
from gateway.http.model_catalog import (
    CatalogAppState,
    CatalogLoadFailure,
    ModelCatalogUnavailableError,
    load_model_catalog,
)
from gateway.http.process import GatewayProcess
from gateway.inspect import okf_graph as okf_graph_router
from gateway.inspect import router as inspect_router
from gateway.lgtm.telemetry_staleness import TelemetryStaleness
from gateway.mcp_server import endpoint as mcp_server_endpoint
from gateway.routers import (
    commands as commands_router,
)
from gateway.routers import (
    frontend_telemetry as frontend_telemetry_router,
)
from gateway.routers import (
    guide as guide_router,
)
from gateway.routers import host_permissions as host_permissions_router
from gateway.routers import (
    notifications as notifications_router,
)
from gateway.routers import (
    settings as settings_router,
)
from gateway.routers import (
    shell as shell_router,
)
from gateway.routers import (
    tasks as tasks_router,
)
from gateway.routers.configuration import runtime as config_router
from gateway.routers.fleet import fleet_graph as fleet_graph_router
from gateway.routers.memory_api import memory as memory_router
from gateway.routers.proxies import grafana as grafana_router
from gateway.routers.proxies import insights as insights_router
from gateway.routers.upload import router as uploads_router
from gateway.schedules import router as schedules_router
from gateway.schedules.session_control import (
    run_plugin_schedules_reconciler,
    sync_startup_schedules,
)
from gateway.schemas.models import HarnessAvailability
from gateway.upload_delivery import router as upload_delivery_router
from gateway.upload_delivery.worker import UploadRecovery
from ops.cluster.rpc import RpcClient

_log = logging.getLogger(__name__)


# meiva:note
# What this root serves with: the base `GatewayAppState` and every directory's subclass of it, so
# each directory's typed read (`state_as(request, ThatSubclass)`) finds its attributes on the one
# object the root assigns them to.
class GatewayServerState(
    CatalogAppState,
    inspect_router.InspectAppState,
    fleet_graph_router.FleetGraphAppState,
    frontend_telemetry_router.FrontendTelemetryAppState,
    grafana_router.GrafanaAppState,
    insights_router.InsightsAppState,
    memory_router.MemoryAppState,
    cluster_router.HarnessAppState,
    upload_delivery_router.UploadAppState,
    status_router.StatusAppState,
    reply_suggestion_router.ReplySuggestionAppState,
    agents_transfer_router.TransferAppState,
    auth_router.AuthAppState,
):
    pass


# meiva:note
# Release what start-up acquired, last acquired first, on success and on failure.
#
# A release that fails does not skip the ones before it. When the body raised, that error
# is the one that propagates and a failing release is added to it as a note; when the body
# finished, the failing release's error propagates.
@asynccontextmanager
async def startup_releases() -> AsyncGenerator[AsyncExitStack]:
    releases = AsyncExitStack()
    try:
        yield releases
    except BaseException as primary:
        try:
            await releases.aclose()
        except BaseException as cleanup:
            primary.add_note(f"gateway start-up cleanup also failed: {cleanup!r}")
        raise
    await releases.aclose()


def _build_request_resources(
    app: FastAPI, process: GatewayProcess, releases: AsyncExitStack
) -> None:
    state = app_state_as(app, GatewayServerState)

    config = process.config
    machine_token_acceptance: AcceptanceCache = {}
    state.machine_token_acceptance = machine_token_acceptance
    state.telemetry_staleness = TelemetryStaleness()
    state.status_cache = StatusCache()
    state.shell_backend = get_shell_backend(home=process.home)
    state.harness_cache = HarnessProbeCache()

    async def _ensure_harnesses(target: str) -> list[HarnessAvailability]:
        return await ensure_machine_harnesses(
            target,
            cache=state.harness_cache,
            db=state.db,
            local_machine=state.machine,
            rpc=state.rpc,
        )

    state.ensure_machine_harnesses = _ensure_harnesses
    state.identity_mismatch_log = IdentityMismatchLog()
    state.service_endpoints = ServiceEndpoints.from_boot(
        config, runtime=paths.run_dir(process.home)
    )
    inspect_queries = inspect_router.build_query_cache()
    state.inspect_query_cache = inspect_queries
    releases.push_async_callback(inspect_queries.aclose)
    gateway: GatewayConfig = state.gateway_config
    state.reply_suggestions = ReplySuggestionService(
        llm_override=gateway.models.llm_override, transport=None
    )
    state.reply_suggestion_policy = reply_suggestion_policy(config)
    state.reply_suggestion_recall = memory_router.recall_for_suggestion
    state.gateway_access = gateway_access_from_boot(config)
    state.memory_search_gate = memory_router.build_search_gate(
        config.view.services.memory_search_max_concurrency
    )

    state.auth401_log = rejection_log.AuthRejectionLog()
    state.login_limiter = LoginRateLimiter(
        LoginRateLimitConfig(
            max_failures=config.view.gateway.login_max_failures,
            lockout_seconds=config.view.gateway.login_lockout_seconds,
        )
    )

    insights_client = insights_router.build_client(process.home)
    state.insights_client = insights_client
    releases.push_async_callback(insights_client.aclose)


@asynccontextmanager
async def lifespan(
    app: FastAPI,
    *,
    process: GatewayProcess,
    process_ownership: Literal["borrowed", "owned"],
    runtime_process: psutil.Process,
) -> AsyncGenerator[None]:
    state = app_state_as(app, GatewayServerState)

    if process_ownership not in {"borrowed", "owned"}:
        raise ValueError("gateway process ownership must be borrowed or owned")
    state.gateway_process = process
    state.machine = process.machine
    state.host = process.host
    with process.lifetime() if process_ownership == "owned" else nullcontext(process):
        config = process.config
        state.process_image = process.image
        telemetry.init_telemetry(
            home=process.home,
            process="gateway",
            pipeline=process.event_pipeline(),
            machine_reader=process.machine.name,
        )
        async with startup_releases() as releases:
            state.config_authority = ConfigAuthority(
                runtime=config.view,
                all_domains=config.view if config.view.profile is None else Settings(profile=None),
                env_path=process.home / ".env",
            )

            state.rpc = RpcClient.from_boot(
                config, timers=SYSTEM_TIMERS, rng=state.gateway_config.rng
            )
            state.started_at = state.clock.now().timestamp()
            if state.gateway_config.human_auth == "trusted_network":
                _log.warning(
                    "gateway human_auth=trusted_network: the API does not authenticate human "
                    "callers; any client that can reach the listener is served as the cluster "
                    "administrator. Keep MEIVA_MACHINE_HOST on a trusted private network address."
                )
            state.db = process.database()
            state.bus = EventBus(
                event_bus_config_from_config(process.config),
                rng=state.gateway_config.rng,
                timers=SYSTEM_TIMERS,
            )
            db_pool = state.db.pool(max_size=state.gateway_config.db_pool_max_size)
            state.db_pool = db_pool
            releases.callback(db_pool.close)
            await asyncio.to_thread(plugins_router.initialize_plugins, db_pool, home=process.home)
            state.sessions = SessionStore(db_pool)
            state.session_keys = SessionKeys(state.gateway_config.rng)
            state.login_passwords = LoginPasswords(state.gateway_config.rng)
            state.session_touch = SessionTouchThrottle()
            state.telemetry_rate_limiter = frontend_telemetry_router.SessionRateLimiter()

            control_db_pool = state.db.pool(min_size=1, max_size=2, timeout=2.0)
            state.control_db_pool = control_db_pool
            releases.callback(control_db_pool.close)

            _build_request_resources(app, process, releases)

            await sync_startup_schedules(
                db_pool,
                home=process.home,
                resolve_machine=process.machine.name,
                provision_builtins=config.view.gateway.provision_builtin_schedules,
            )

            grafana_client = grafana_router.build_proxy_client()
            state.grafana_client = grafana_client
            releases.push_async_callback(grafana_client.aclose)

            try:
                # meiva:note
                # The job is anchored to the home this entry resolved, the checkout this module was
                # imported from, and the account home the root read once into the app config
                # (`user_home`: the folder a Desktop/Documents/Downloads permission grant names).
                await asyncio.to_thread(
                    register_os_cron,
                    enabled=config.view.general.os_jobs_enabled,
                    host=process.host,
                    roots=JobRoots(
                        home=process.home,
                        repo=paths.repo_root(),
                        user_home=state.gateway_config.user_home,
                    ),
                )
            except Exception:
                _log.warning("OS health-probe cron registration failed", exc_info=True)

            monitor = runtime_metrics.start_runtime_monitor(
                process=runtime_process, policy=runtime_metrics.DEFAULT_LAG_POLICY
            )
            state.runtime_metrics = monitor
            releases.callback(monitor.stop)

            upload_recovery = UploadRecovery(
                lambda: state.db_pool,
                state.db,
                state.bus,
                machine=process.machine,
                home=process.home,
                claim_seconds=config.view.gateway.upload_recovery_claim_seconds,
                rpc=state.rpc,
            )
            state.upload_recovery = upload_recovery

            async with _background_lifetime(
                app,
                upload_recovery,
                outbox_interval_seconds=config.view.gateway.notification_outbox_interval_seconds,
                outbox_batch=config.view.gateway.notification_outbox_batch,
            ):
                yield


@asynccontextmanager
async def _background_lifetime(
    app: FastAPI,
    upload_recovery: UploadRecovery,
    *,
    outbox_interval_seconds: float,
    outbox_batch: int,
) -> AsyncGenerator[None]:
    state = app_state_as(app, GatewayServerState)

    mcp_manager = None
    config: GatewayConfig = state.gateway_config
    if config.mcp_endpoint_enabled:
        mcp_manager = mcp_server_endpoint.build_manager(app)
        state.mcp_manager = mcp_manager

    try:
        async with asyncio.TaskGroup() as background:
            latency_flusher = background.create_task(
                latency.latency_flusher(), name="gateway-latency-flusher"
            )
            auth401_flusher = background.create_task(
                rejection_log.auth401_flusher(state.auth401_log),
                name="gateway-auth-flusher",
            )
            outbox = background.create_task(
                notification_outbox.run(
                    state.db_pool,
                    state.bus,
                    interval_seconds=outbox_interval_seconds,
                    batch=outbox_batch,
                ),
                name="gateway-notification-outbox",
            )
            plugin_schedules_reconciler = background.create_task(
                run_plugin_schedules_reconciler(
                    state.db_pool,
                    state.gateway_process.home,
                    resolve_machine=state.gateway_process.machine.name,
                    provision_builtins=state.gateway_process.config.view.gateway.provision_builtin_schedules,
                ),
                name="gateway-plugin-schedules-reconciler",
            )
            if config.upload_recovery_enabled:
                upload_recovery.start(background)
            try:
                if mcp_manager is not None:
                    async with mcp_manager.run():
                        yield
                else:
                    yield
            finally:
                latency_flusher.cancel()
                auth401_flusher.cancel()
                outbox.cancel()
                plugin_schedules_reconciler.cancel()
                await upload_recovery.close()
    finally:
        state.mcp_manager = None


_OPENAPI_DOCUMENT = TypeAdapter(dict[str, object])


# meiva:link gateway/docs/error-responses-and-middleware-order.meiva.okf.md
def create_app(
    config: GatewayConfig,
    *,
    process: GatewayProcess,
    process_ownership: Literal["borrowed", "owned"],
    catalog: ModelCatalog | CatalogLoadFailure,
) -> FastAPI:

    if process_ownership not in {"borrowed", "owned"}:
        raise ValueError("gateway process ownership must be borrowed or owned")
    app = FastAPI(
        title="Meiva",
        lifespan=partial(
            lifespan,
            process=process,
            process_ownership=process_ownership,
            # meiva:note
            # The process the runtime monitor samples is this gateway's own; the root builds it here
            # so no async body reaches for psutil.
            runtime_process=psutil.Process(),
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    state = GatewayServerState()
    app.state = state
    state.gateway_config = config
    state.clock = Clock(config.clock, timers=SYSTEM_TIMERS)
    state.catalog = catalog
    if isinstance(catalog, ModelCatalog):
        state.memory_embedder = memory_router.build_embedder(config.memory_search, catalog)
    state.command_catalog = CommandCatalog.system(
        skills=partial(
            load_skill_names,
            home=process.home,
            environ=process.config.process_environment(),
        ),
        home=process.home,
    )
    state.memory_unreadable_reports = memory_router.unreadable_reports()
    state.gateway_process = process
    machine_token_acceptance: AcceptanceCache = {}
    state.machine_token_acceptance = machine_token_acceptance
    state.agent_transfer_bundles = TransferBundleStore()
    app.middleware("http")(cluster_auth_middleware)

    app.middleware("http")(latency.latency_middleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.middleware("http")(request_trace_middleware(config.rng))

    app.add_exception_handler(MeivaAgentError, meiva_agent_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.add_exception_handler(ModelCatalogUnavailableError, model_catalog_unavailable_handler)

    for router in (
        auth_router.router,
        bootstrap_router.router,
        agents_router.router,
        to_user_router.router,
        agents_launch_retry_router.router,
        agents_lifecycle_router.router,
        agents_transfer_router.router,
        agents_understanding_router.router,
        agents_state_router.router,
        agent_events_router.router,
        computer_traces_router.router,
        inspect_router.router,
        shell_router.router,
        timeline_router.router,
        reply_suggestion_router.router,
        user_delivery_router.router,
        asks_router.router,
        suggestion_feedback_router.router,
        system_router.router,
        host_permissions_router.router,
        notices_router.router,
        guarded_notices_router.router,
        cluster_router.router,
        machine_pause_router.router,
        commands_router.router,
        config_router.router,
        conversation_router.router,
        settings_router.router,
        notifications_router.router,
        schedules_router.router,
        guide_router.router,
        inventory_router.router,
        plugins_router.router,
        skills_router.router,
        extensions_registry_router.router,
        packages_router.router,
        metrics_router.router,
        events_router.router,
        merged_stream_router.router,
        insights_router.router,
        event_resolutions_router.router,
        ops_monitor_router.router,
        alerts_router.router,
        status_router.router,
        alert_classes_router.router,
        memory_router.router,
        fleet_graph_router.router,
        frontend_telemetry_router.router,
        grafana_router.router,
        okf_graph_router.router,
        tasks_router.router,
        task_assignments_router.router,
        plugin_ui_router.router,
        ui_contributions_router.router,
        uploads_router.router,
        upload_delivery_router.router,
    ):
        app.include_router(router)

    app.mount("/mcp", mcp_server_endpoint.mcp_gateway(app))
    return app


class UnpricedDefaultModelError(RuntimeError):
    pass


# meiva:note
# The app's configuration as this process root builds it: the account home is read here, once; the
# memory directory is derived from the Meiva home the entry resolved and hands in; and the entropy
# every request id, attempt id, upload batch id and session key is drawn from is the operating
# system's.
def process_config(config: ConfigBoot, *, home: Path) -> GatewayConfig:
    return gateway_config_from_boot(
        config,
        user_home=Path.home(),
        home=home,
        memory_root=paths.gateway_memory_dir(
            roles=ConfiguredMachineIdentity(config).role(), home=home
        ),
        rng=secrets.SystemRandom(),
    )


# meiva:note
# The cluster default model is named by config, not by a spawn request, so a missing price there
# has no overlay to refuse. Fail the catalog instead of the process: model routes refuse with a
# repair message while gateway management stays available, and nobody is promised a priced bill
# the catalog cannot price.
def catalog_with_priced_default(
    *, build: Callable[[], ModelCatalog], default_model: str
) -> ModelCatalog:
    catalog = build()
    if default_model not in catalog.prices:
        raise UnpricedDefaultModelError(
            f"cluster default model {default_model!r} has no price in the pricing catalog; "
            "usage for it would be recorded with cost 0, so the bill would silently undercount. "
            "Land the price in base/lm/pricing/pricing_catalog_archive.json (or the plugin price "
            "registry), or point the config at a priced model, then restart the gateway."
        )
    return catalog


def create_app_from_environment() -> FastAPI:

    config = ConfigBoot()
    config.boot()
    config.ensure_eager()
    home = paths.meiva_home()
    process = GatewayProcess(
        home=home,
        config=config,
        image=LOADED_IMAGE,
        machine=ConfiguredMachineIdentity(config),
        host=Host.system(environ=config.process_environment()),
    )
    gateway_config_value = process_config(process.config, home=home)
    return create_app(
        gateway_config_value,
        process=process,
        process_ownership="owned",
        catalog=load_model_catalog(
            partial(
                catalog_with_priced_default,
                build=partial(build_model_catalog, home=home),
                default_model=gateway_config_value.models.llm_model,
            )
        ),
    )


def openapi_schema() -> dict[str, object]:

    return openapi_schema_of(ConfigBoot(), home=paths.resolve_meiva_home())


# meiva:note
# The OpenAPI document of an app built cold: no configuration field is read.
def openapi_schema_of(config: ConfigBoot, *, home: Path) -> dict[str, object]:
    process = GatewayProcess(
        home=home,
        config=config,
        image=LOADED_IMAGE,
        machine=ConfiguredMachineIdentity(config),
        host=Host.system(environ=config.process_environment()),
    )
    with process.lifetime():
        app = create_app(
            placeholder_config(),
            process=process,
            process_ownership="borrowed",
            catalog=CatalogLoadFailure(RuntimeError("the schema app serves no model operation")),
        )
        return _OPENAPI_DOCUMENT.validate_python(app.openapi())


def _borrowed_app(process: GatewayProcess) -> FastAPI:
    return create_app(
        process_config(process.config, home=process.home),
        process=process,
        process_ownership="borrowed",
        catalog=load_model_catalog(partial(build_model_catalog, home=process.home)),
    )


def main() -> None:

    config = ConfigBoot()
    config.boot()
    config.ensure_eager()
    _run_gateway(home=paths.meiva_home(), config=config, build_app=_borrowed_app)
