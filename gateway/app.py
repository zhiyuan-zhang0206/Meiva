import asyncio
import logging
import secrets
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext
from functools import partial
from pathlib import Path
from typing import Any, Literal

import psutil
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
from base.host.net.auth import cookie_name
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
from gateway.agents.reply_suggestion import router as reply_suggestion_router
from gateway.agents.reply_suggestion.configuration import reply_suggestion_policy
from gateway.agents.reply_suggestion.service import ReplySuggestionService
from gateway.agents.task_assignment import router as task_assignments_router
from gateway.agents.to_user import document as to_user_router
from gateway.agents.transfer import router as agents_transfer_router
from gateway.agents.transfer.bundle_store import TransferBundleStore
from gateway.alerts import router as alerts_router
from gateway.cluster import bootstrap as bootstrap_router
from gateway.cluster import machine_pause as machine_pause_router
from gateway.cluster import router as cluster_router
from gateway.cluster import status as status_router
from gateway.cluster.harness_probe import HarnessProbeCache
from gateway.cluster.monitor import alert_classes as alert_classes_router
from gateway.cluster.monitor import ops_monitor as ops_monitor_router
from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess
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
from gateway.extensions import registry as extensions_registry_router
from gateway.extensions import skills as skills_router
from gateway.extensions import ui_contributions as ui_contributions_router
from gateway.http.auth import rejection_log
from gateway.http.auth import router as auth_router
from gateway.http.auth.request_principal import TRUSTED_NETWORK_FACT, SessionKeys
from gateway.http.auth.session_store import SessionStore, SessionTouchThrottle, touch_session
from gateway.http.config import GatewayConfig, gateway_config_from_boot, placeholder_config
from gateway.http.middleware import latency, runtime_metrics
from gateway.http.middleware.cors import request_host
from gateway.http.middleware.error_envelope import error_response, request_trace_middleware
from gateway.http.middleware.error_handlers import (
    http_exception_handler,
    meiva_agent_error_handler,
    model_catalog_unavailable_handler,
    request_validation_error_handler,
    unhandled_exception_handler,
)
from gateway.http.model_catalog import (
    CatalogLoadFailure,
    ModelCatalogUnavailableError,
    load_model_catalog,
)
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
from gateway.routers.configuration import default_model as default_model_router
from gateway.routers.configuration import runtime as config_router
from gateway.routers.fleet import fleet_graph as fleet_graph_router
from gateway.routers.memory_api import memory as memory_router
from gateway.routers.page_serving import page_target
from gateway.routers.page_serving import pages as pages_router
from gateway.routers.proxies import grafana as grafana_router
from gateway.routers.proxies import insights as insights_router
from gateway.routers.upload import router as uploads_router
from gateway.schedules import router as schedules_router
from gateway.schedules.session_control import (
    run_plugin_schedules_reconciler,
    sync_startup_schedules,
)
from gateway.upload_delivery import router as upload_delivery_router
from gateway.upload_delivery.worker import UploadRecovery
from ops.cluster.rpc import RpcClient

_log = logging.getLogger(__name__)


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

    config = process.config
    machine_token_acceptance: AcceptanceCache = {}
    app.state.machine_token_acceptance = machine_token_acceptance
    app.state.telemetry_staleness = TelemetryStaleness()
    app.state.status_cache = StatusCache()
    app.state.shell_backend = get_shell_backend(home=process.home)
    app.state.harness_cache = HarnessProbeCache()
    app.state.identity_mismatch_log = IdentityMismatchLog()
    app.state.service_endpoints = ServiceEndpoints.from_boot(
        config, runtime=paths.run_dir(process.home)
    )
    inspect_queries = inspect_router.build_query_cache()
    app.state.inspect_query_cache = inspect_queries
    releases.push_async_callback(inspect_queries.aclose)
    gateway: GatewayConfig = app.state.gateway_config
    app.state.reply_suggestions = ReplySuggestionService(
        llm_override=gateway.models.llm_override, transport=None
    )
    app.state.reply_suggestion_policy = reply_suggestion_policy(config)
    app.state.gateway_access = gateway_access_from_boot(config)
    app.state.memory_search_gate = memory_router.build_search_gate(
        config.view.services.memory_search_max_concurrency
    )

    app.state.auth401_log = rejection_log.AuthRejectionLog()
    app.state.login_limiter = LoginRateLimiter(
        LoginRateLimitConfig(
            max_failures=config.view.gateway.login_max_failures,
            lockout_seconds=config.view.gateway.login_lockout_seconds,
        )
    )

    insights_client = insights_router.build_client(process.home)
    app.state.insights_client = insights_client
    releases.push_async_callback(insights_client.aclose)


@asynccontextmanager
async def lifespan(
    app: FastAPI,
    *,
    process: GatewayProcess,
    process_ownership: Literal["borrowed", "owned"],
    runtime_process: psutil.Process,
) -> AsyncGenerator[None]:

    if process_ownership not in {"borrowed", "owned"}:
        raise ValueError("gateway process ownership must be borrowed or owned")
    app.state.gateway_process = process
    app.state.machine = process.machine
    app.state.host = process.host
    with process.lifetime() if process_ownership == "owned" else nullcontext(process):
        config = process.config
        app.state.process_image = process.image
        telemetry.init_telemetry(
            home=process.home,
            process="gateway",
            pipeline=process.event_pipeline(),
            machine_reader=process.machine.name,
        )
        async with startup_releases() as releases:
            app.state.config_authority = ConfigAuthority(
                runtime=config.view,
                all_domains=config.view if config.view.profile is None else Settings(profile=None),
                env_path=process.home / ".env",
            )

            app.state.rpc = RpcClient.from_boot(
                config, timers=SYSTEM_TIMERS, rng=app.state.gateway_config.rng
            )
            app.state.started_at = app.state.clock.now().timestamp()
            if app.state.gateway_config.human_auth == "trusted_network":
                _log.warning(
                    "gateway human_auth=trusted_network: the API does not authenticate human "
                    "callers; any client that can reach the listener is served as the cluster "
                    "administrator. Keep MEIVA_MACHINE_HOST on a trusted private network address."
                )
            app.state.db = process.database()
            app.state.bus = EventBus(
                event_bus_config_from_config(process.config),
                rng=app.state.gateway_config.rng,
                timers=SYSTEM_TIMERS,
            )
            db_pool = app.state.db.pool(max_size=app.state.gateway_config.db_pool_max_size)
            app.state.db_pool = db_pool
            releases.callback(db_pool.close)
            app.state.sessions = SessionStore(db_pool)
            app.state.session_keys = SessionKeys(app.state.gateway_config.rng)
            app.state.session_touch = SessionTouchThrottle()
            app.state.page_host_cache = page_target.PageHostCache()
            app.state.telemetry_rate_limiter = frontend_telemetry_router.SessionRateLimiter()

            control_db_pool = app.state.db.pool(min_size=1, max_size=2, timeout=2.0)
            app.state.control_db_pool = control_db_pool
            releases.callback(control_db_pool.close)

            _build_request_resources(app, process, releases)

            await sync_startup_schedules(
                db_pool,
                home=process.home,
                resolve_machine=process.machine.name,
                provision_builtins=config.view.gateway.provision_builtin_schedules,
            )

            grafana_client = grafana_router.build_proxy_client()
            app.state.grafana_client = grafana_client
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
                        user_home=app.state.gateway_config.user_home,
                    ),
                )
            except Exception:
                _log.warning("OS health-probe cron registration failed", exc_info=True)

            monitor = runtime_metrics.start_runtime_monitor(
                process=runtime_process, policy=runtime_metrics.DEFAULT_LAG_POLICY
            )
            app.state.runtime_metrics = monitor
            releases.callback(monitor.stop)

            upload_recovery = UploadRecovery(
                app.state.db_pool,
                app.state.db,
                app.state.bus,
                machine=process.machine,
                home=process.home,
                claim_seconds=config.view.gateway.upload_recovery_claim_seconds,
                rpc=app.state.rpc,
            )
            app.state.upload_recovery = upload_recovery

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

    mcp_manager = None
    config: GatewayConfig = app.state.gateway_config
    if config.mcp_endpoint_enabled:
        mcp_manager = mcp_server_endpoint.build_manager(app)
        app.state.mcp_manager = mcp_manager

    try:
        async with asyncio.TaskGroup() as background:
            latency_flusher = background.create_task(
                latency.latency_flusher(), name="gateway-latency-flusher"
            )
            auth401_flusher = background.create_task(
                rejection_log.auth401_flusher(app.state.auth401_log),
                name="gateway-auth-flusher",
            )
            outbox = background.create_task(
                notification_outbox.run(
                    app.state.db_pool,
                    app.state.bus,
                    interval_seconds=outbox_interval_seconds,
                    batch=outbox_batch,
                ),
                name="gateway-notification-outbox",
            )
            plugin_schedules_reconciler = background.create_task(
                run_plugin_schedules_reconciler(app.state.db_pool, app.state.gateway_process.home),
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
        app.state.mcp_manager = None


_AUTH_BYPASS_PATHS: frozenset[str] = frozenset(
    {
        "/api/health",
        "/api/auth/login",
        "/api/auth/login-ticket/redeem",
        "/api/auth/check",
        "/api/auth/logout",
    }
)
_AUTH_BYPASS_METHOD_PATHS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/alerts"),
    }
)
_STATE_CHANGING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


async def _cookie_session(request: Request, secret: str) -> tuple[str, str] | None:

    from gateway.http.auth.request_principal import current_session_fact

    cookie_token = request.cookies.get(cookie_name())
    if not cookie_token:
        return None
    fact = await asyncio.to_thread(
        current_session_fact,
        request.app.state.sessions,
        request.app.state.session_keys,
        cookie_token,
        secret,
        home=request.app.state.gateway_process.home,
        now=request.app.state.clock.now(),
        cache=request.app.state.machine_token_acceptance,
    )
    return None if fact is None else (cookie_token, fact)


# meiva:note
# True for a state-changing request whose `Origin` is not an allowed browser origin.
def _foreign_origin(request: Request, config: GatewayConfig) -> bool:
    origin = request.headers.get("Origin")
    return (
        request.method in _STATE_CHANGING_METHODS
        and origin is not None
        and origin not in config.allowed_origins
    )


def _origin_refused() -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"detail": "origin not allowed"},
        headers={"Vary": "Origin"},
    )


# meiva:note
# With `human_auth = trusted_network`, a request that presents no valid session cookie and
# no Authorization header at all is served as the cluster administrator (fact `trusted_network`). A browser page is
# the one way to reach that listener without the operator choosing to, so these anonymous
# requests, and only these, also face the Origin and Host checks. An Authorization header
# that does not verify is a client error and stays a 401, so a misconfigured machine fails
# now instead of working silently until the mode is switched back. A stale session cookie
# is not: a browser cannot clear it.
# meiva:link gateway/http/auth/docs/web-sessions.meiva.okf.md
async def cluster_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:

    from gateway.http.auth.request_principal import AuthPrincipal, cluster_credential

    request.state.auth_principal = None
    request.state.source_verified_by = None
    config: GatewayConfig = request.app.state.gateway_config
    secret = config.cluster_secret

    if request.method == "OPTIONS":
        return await call_next(request)
    if (
        request.url.path in _AUTH_BYPASS_PATHS
        or (
            request.method,
            request.url.path,
        )
        in _AUTH_BYPASS_METHOD_PATHS
    ):
        return await call_next(request)

    session = await _cookie_session(request, secret)
    if session is not None:
        cookie_token, session_fact = session
        request.state.auth_principal = AuthPrincipal("cluster", "administrator")
        request.state.source_verified_by = session_fact
        if _foreign_origin(request, config):
            return _origin_refused()
        touch: SessionTouchThrottle = request.app.state.session_touch
        if touch.due(
            cookie_token,
            request.app.state.clock.monotonic(),
            stale_after_s=config.session_ttl_seconds,
        ):
            await asyncio.to_thread(
                touch_session,
                request.app.state.db_pool,
                cookie_token,
            )
        return await call_next(request)

    verified_by = cluster_credential(
        request.headers.get("Authorization"),
        secret,
        home=request.app.state.gateway_process.home,
        cache=request.app.state.machine_token_acceptance,
    )
    if verified_by is not None:
        request.state.auth_principal = AuthPrincipal("cluster", "administrator")
        request.state.source_verified_by = verified_by
        return await call_next(request)

    if config.human_auth == "trusted_network" and request.headers.get("Authorization") is None:
        if _foreign_origin(request, config):
            return _origin_refused()
        if request_host(request.headers.get("Host")) not in config.trusted_hosts:
            return JSONResponse(status_code=403, content={"detail": "host not allowed"})
        request.state.auth_principal = AuthPrincipal("cluster", "administrator")
        request.state.source_verified_by = TRUSTED_NETWORK_FACT
        return await call_next(request)

    request.app.state.auth401_log.log(request, request.app.state.clock.monotonic())
    return error_response(
        request,
        code="authentication_required",
        status=401,
        detail="authentication required",
        retryable=False,
    )


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
    app.state.gateway_config = config
    app.state.clock = Clock(config.clock, timers=SYSTEM_TIMERS)
    app.state.catalog = catalog
    if isinstance(catalog, ModelCatalog):
        app.state.memory_embedder = memory_router.build_embedder(config.memory_search, catalog)
    app.state.command_catalog = CommandCatalog.system(
        skills=partial(load_skill_names, home=process.home), home=process.home
    )
    app.state.memory_unreadable_reports = memory_router.unreadable_reports()
    app.state.gateway_process = process
    machine_token_acceptance: AcceptanceCache = {}
    app.state.machine_token_acceptance = machine_token_acceptance
    app.state.agent_transfer_bundles = TransferBundleStore()
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
        system_router.router,
        host_permissions_router.router,
        pages_router.router,
        notices_router.router,
        guarded_notices_router.router,
        cluster_router.router,
        machine_pause_router.router,
        commands_router.router,
        config_router.router,
        conversation_router.router,
        default_model_router.router,
        settings_router.router,
        notifications_router.router,
        schedules_router.router,
        guide_router.router,
        inventory_router.router,
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


def openapi_schema() -> dict[str, Any]:

    return openapi_schema_of(ConfigBoot(), home=paths.resolve_meiva_home())


# meiva:note
# The OpenAPI document of an app built cold: no configuration field is read.
def openapi_schema_of(config: ConfigBoot, *, home: Path) -> dict[str, Any]:
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
        return app.openapi()


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
