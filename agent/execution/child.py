# meiva:note
# The child preserves the code-reached distinction and a full crash envelope, including a fallback when the normal protocol writer fails.
# meiva:link agent/execution/docs/child-boundaries.meiva.okf.md

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, TypeGuard

from agent.execution.delivery_plan import (
    delivers_telemetry,
    plan_finalize,
    rewrites_result_after_delivery_failure,
)
from agent.execution.protocol import RequestPayload, ResultPayload
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.host.env.agent_env import AGENT_ID_ENV
from base.log import logger

WATCHDOG_MARGIN_S = 5.0


WATCHDOG_EXIT_CODE = 124

_RESULT_KIND: dict[type[BaseException], Literal["cancelled", "timed_out"]] = {
    KeyboardInterrupt: "cancelled",
    TimeoutError: "timed_out",
}


# meiva:note
# The one thing the framework overlay needs from the boot configuration. Importing `base.config`
# boots the lite configuration, and the entry reports a boot failure as a crash envelope, so the
# module cannot name `ConfigBoot` at import time: `main` imports it inside its guard and the child
# names the capability here.
class _FieldSetter(Protocol):
    def set_field(self, name: str, value: object) -> None: ...


class _Sdk(Protocol):
    state: object
    state_update: dict[str, Any]

    def ensure_plugins_loaded(
        self, *, surface: bool, validate_state: Callable[[Any], object]
    ) -> None: ...


@dataclass(frozen=True)
class ChildContext:
    meiva: _Sdk
    boot_started_at: float


def import_runtime(boot_started_at: float) -> ChildContext:
    import meiva

    return ChildContext(meiva=meiva, boot_started_at=boot_started_at)


def line_buffered_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(line_buffering=True)


def _install_signal_handlers() -> None:

    def _raise_keyboard_interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    def _raise_timeout_error(_signum: int, _frame: object) -> None:
        raise TimeoutError("exec subprocess timed out")

    signal.signal(signal.SIGINT, _raise_keyboard_interrupt)
    signal.signal(signal.SIGTERM, _raise_timeout_error)


def _arm_watchdog(timeout_s: float) -> None:
    from base.native_process.exec_domain import KILL_GRACE_S

    margin = float(os.environ.get("MEIVA_EXEC_WATCHDOG_MARGIN_S", WATCHDOG_MARGIN_S))
    delay = timeout_s + KILL_GRACE_S + margin

    def _timeout() -> None:
        os._exit(WATCHDOG_EXIT_CODE)

    timer = threading.Timer(delay, _timeout)

    timer.daemon = True
    timer.start()


def _is_object_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def pop_overlay_env() -> tuple[dict[str, object] | None, dict[str, object] | None]:
    import json as _json

    from base.host.env.agent_env import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV

    maps: dict[str, dict[str, object] | None] = {}
    for env_name in (AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV):
        raw = os.environ.pop(env_name, "")
        if not raw:
            maps[env_name] = None
            continue
        value: object = _json.loads(raw)
        if not _is_object_dict(value):
            raise TypeError(f"{env_name} must be a JSON object, got {type(value).__name__}")
        maps[env_name] = {str(k): v for k, v in value.items()}
    return (
        maps[AGENT_BIRTH_CONFIG_ENV],
        maps[AGENT_CONFIG_OVERLAY_ENV],
    )


def apply_framework_overlay(
    birth: dict[str, object] | None, overlay: dict[str, object] | None, *, config: _FieldSetter
) -> bool:
    applied = False
    for value in (birth, overlay):
        if value:
            from pydantic import ValidationError

            from base.packages.plugins.config_registration import plan_config_overlay
            from base.packages.plugins.plugin_errors import InvalidConfigOverlay

            for field, field_value in plan_config_overlay(value).framework_fields:
                try:
                    config.set_field(field, field_value)
                except ValidationError as error:
                    raise InvalidConfigOverlay(
                        f"overlay field type validation failed (framework): {error}"
                    ) from error
            applied = True
    return applied


def apply_plugin_overlay(
    birth: dict[str, object] | None, overlay: dict[str, object] | None
) -> None:
    for value in (birth, overlay):
        if value:
            from meiva.sdk_surface import install

            install.apply_config_overlay(value)


def init_logger(
    agent_id: int | None,
    *,
    producer: Callable[[], Any],
    machine_reader: Callable[[], str],
    environ: Mapping[str, str],
) -> None:
    if agent_id is None:
        return
    from base.log.process import init_subprocess_logger

    init_subprocess_logger(agent_id=agent_id)
    try:
        from base.telemetry.process_logging import add_event_sink

        add_event_sink(
            process="agent-exec",
            agent_id=agent_id,
            producer=producer,
            machine_reader=machine_reader,
        )
    except Exception:
        logger.warning(
            "[exec-child] event pipeline sink unavailable — sdk_call events "
            "for this exec reach the file sink only",
            agent_id=agent_id,
        )
        return
    from base.telemetry.delivery.defer_plan import child_defer_plan

    producer().defer_exports(child_defer_plan(environ))


def emit_child_boot_timing(child: ChildContext, *, clients: ClientSet, now: float) -> None:
    duration_ms = (now - child.boot_started_at) * 1000
    extra: dict[str, object] = {}
    deferred = clients.exports_deferred()
    if deferred is not None:
        extra["otlp_deferred"] = deferred
    logger.info(
        "exec child boot completed in {duration_ms:.1f}ms",
        event="exec_child_boot",
        duration_ms=duration_ms,
        **extra,
    )


class _LazyStateSlot:
    def __init__(self, payload: RequestPayload, meiva: _Sdk) -> None:
        object.__setattr__(self, "_payload", payload)
        object.__setattr__(self, "_meiva", meiva)
        object.__setattr__(self, "_real", None)

    def materialize(self) -> None:
        if object.__getattribute__(self, "_real") is not None:
            return

        meiva: _Sdk = object.__getattribute__(self, "_meiva")
        from agent.state import build_agent_state, plugin_state_schema

        meiva.ensure_plugins_loaded(surface=False, validate_state=plugin_state_schema)
        payload: RequestPayload = object.__getattribute__(self, "_payload")
        from base.packages.plugins.loading.registry import build_registry

        state_cls = build_agent_state(build_registry(validate_state=plugin_state_schema))
        snapshot = payload.materialize_state()
        real = state_cls.model_validate(snapshot)
        object.__setattr__(self, "_real", real)
        meiva.state = real

    def __getattr__(self, name: str) -> object:
        self.materialize()
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name: str, value: object) -> None:
        self.materialize()
        setattr(object.__getattribute__(self, "_real"), name, value)


def build_state_slot(child: ChildContext, payload: RequestPayload) -> None:
    if payload.state_raw is None:
        return
    child.meiva.state = _LazyStateSlot(payload, child.meiva)
    child.meiva.state_update = {}


def _take_result_state_update(
    child: ChildContext, payload: ResultPayload, *, state_injected: bool
) -> None:
    if not state_injected:
        return
    update = child.meiva.state_update
    if not isinstance(update, dict):
        payload.state_update_error = (
            f"plugin tampered with meiva.state_update: expected dict, got {type(update).__name__}"
        )
        return

    payload.state_update = update or None


# meiva:note
# The machine's shared applications must outlive a runaway exec. systemd hands user units
# oom_score_adj=300 (browser, editor, input method), so a worker left at the inherited 0 is
# *safer* for the kernel than the apps it starves: on 2026-10-22 an 8.29 GiB export killed
# chrome and typeless on ubuntu while its own score stayed 0 (#5543). Raising only our own
# score puts the kernel's first choice on the process we can simply run again.
OOM_SCORE_ADJ_ENV = "MEIVA_EXEC_OOM_SCORE_ADJ"
DEFAULT_OOM_SCORE_ADJ = 800
OOM_SCORE_ADJ_PATH = Path("/proc/self/oom_score_adj")


# meiva:note
# Raise only this process's score: a value the kernel reads before it decides whom to kill.
# Returns what was written, or None when the preference is off or the file does not exist.
def apply_oom_preference(env: Mapping[str, str], path: Path) -> int | None:
    raw = env.get(OOM_SCORE_ADJ_ENV)
    try:
        score = DEFAULT_OOM_SCORE_ADJ if raw in (None, "") else int(raw)
    except ValueError:
        score = DEFAULT_OOM_SCORE_ADJ
    score = min(max(score, 0), 1000)
    if score == 0:
        return None
    with contextlib.suppress(OSError):
        path.write_text(str(score), encoding="utf-8")
        return score
    return None


def run_code(code: str, payload: ResultPayload) -> None:
    from agent.execution.agent_traceback import (
        format_agent_traceback,
        format_full_traceback,
        register_agent_source,
    )
    from base.agents.sdk import telemetry as sdk_usage_telemetry
    from meiva.sdk_surface import process_context
    from meiva.sdk_surface.help import HelpRouter
    from meiva.sdk_surface.installation import installed

    register_agent_source(code)
    import builtins

    builtins_map: dict[str, Any] = (
        {str(k): v for k, v in __builtins__.items()}
        if _is_object_dict(__builtins__)
        else dict(vars(builtins))
    )
    original_help = builtins_map["help"]
    builtins_map["help"] = HelpRouter(original_help)
    fresh_globals: dict[str, Any] = {
        "__name__": "__agent_code__",
        "__builtins__": builtins_map,
    }
    tally = sdk_usage_telemetry.SdkCallTally()
    execution_context = process_context.current_context()
    installation = installed()
    sampling = (
        contextlib.nullcontext() if installation is None else installation.sampling.execution()
    )
    process_context.bind_context(replace(execution_context, sdk_calls=tally))
    try:
        payload.code_reached = True

        with sampling:
            exec(compile(code, "<agent_code>", "exec"), fresh_globals)
    except BaseException as exc:
        from base.agents.lifecycle import LifecycleExit

        if isinstance(exc, LifecycleExit):
            payload.kind = "lifecycle"
            payload.lifecycle_type = type(exc).__name__
            return

        payload.kind = _RESULT_KIND.get(type(exc), "crashed")
        payload.exc_type = type(exc).__name__
        payload.exc_msg = str(exc)[:2000]
        payload.full_traceback = format_full_traceback(exc)
        sys.stdout.write(format_agent_traceback(exc))
        sys.stdout.flush()
    finally:
        payload.sdk_calls = sdk_usage_telemetry.tally_entries(tally.snapshot())
        process_context.bind_context(execution_context)


def finalize_telemetry(*, clients: ClientSet | None) -> None:
    if "base.telemetry" not in sys.modules:
        return
    from base import telemetry

    owned: telemetry.DrainResult | None = None if clients is None else clients.sync_events()
    plan = plan_finalize(
        owned_unfinished=None
        if owned is None
        else owned.status is telemetry.DrainStatus.UNFINISHED,
        ordinary_unfinished=telemetry.sync().status is telemetry.DrainStatus.UNFINISHED,
    )
    if plan.warn_owned_unfinished:
        logger.warning(
            "exec child: owned telemetry delivery is unfinished; "
            "queued records may be lost or land later"
        )
    if plan.warn_ordinary_unfinished:
        logger.warning(
            "exec child: ordinary telemetry delivery is unfinished; "
            "queued records may be lost or land later"
        )
    if plan.finalize_owned and clients is not None:
        clients.finalize_events()


def deliver_envelope_telemetry(*, clients: ClientSet | None) -> None:
    try:
        finalize_telemetry(clients=clients)
    except BaseException:
        logger.opt(exception=True).warning(
            "exec child: telemetry delivery after a crash envelope failed; "
            "ordinary observation delivery is unfinished; queued records may be lost or land later"
        )


def deliver_run_telemetry(result_path: str, payload: ResultPayload, *, clients: ClientSet) -> None:
    if not delivers_telemetry(payload.kind):
        return
    try:
        finalize_telemetry(clients=clients)
    except BaseException as exc:
        logger.opt(exception=True).warning(
            "exec child: post-run telemetry delivery failed (run outcome: {}); "
            "ordinary observation delivery is unfinished; queued records may be lost or land later",
            payload.kind,
        )

        if rewrites_result_after_delivery_failure(payload.kind):
            write_crashed_result(result_path, exc, code_reached=payload.code_reached)


def run_child(
    request_path: str,
    result_path: str,
    boot_started_at: float,
    *,
    config: object,
    context: MeivaContext,
) -> None:
    child = import_runtime(boot_started_at)
    from agent.execution.protocol import read_request, write_result
    from base.config import ConfigBoot

    if not isinstance(config, ConfigBoot):
        raise TypeError("run_child needs the child's booted ConfigBoot")
    raw_agent_id = os.environ.get(AGENT_ID_ENV)
    init_logger(
        None if raw_agent_id is None else int(raw_agent_id),
        producer=context.clients.event_pipeline,
        machine_reader=context.require_machine().name,
        environ=os.environ,
    )
    request = read_request(Path(request_path))
    payload = ResultPayload(kind="done")

    birth, overlay = pop_overlay_env()
    from base.clock import SYSTEM_TIMERS, Clock
    from base.clock.configuration import clock_config_from_boot

    def clock_factory() -> Clock:
        return Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS)

    from meiva.sdk_surface import process_context

    process_context.bind_context(
        replace(
            MeivaContext.from_description(
                request.context,
                original_incarnation=request.incarnation,
                clients=context.clients,
                machine=context.require_machine(),
            ),
            clock_factory=clock_factory,
        )
    )

    framework_overlay_applied = apply_framework_overlay(birth, overlay, config=config)
    import meiva

    init_logger(
        request.agent_id,
        producer=meiva.context.clients.event_pipeline,
        machine_reader=context.require_machine().name,
        environ=os.environ,
    )
    from meiva.boot import initialize_context

    initialize_context(meiva.context, config=config, clients=meiva.context.clients)
    from meiva.sdk_surface import settings as sdk_settings

    apply_plugin_overlay(birth, overlay)

    def read_agent_default(_domain: str, field: str) -> object:
        return sdk_settings.config_authority().service_field_value(field)

    if framework_overlay_applied:
        from agent.process_boot import apply_per_agent_sdk_disable

        apply_per_agent_sdk_disable(default_reader=read_agent_default)

    from agent.process_boot import apply_per_agent_eval_isolation

    apply_per_agent_eval_isolation(agent_id=request.agent_id, default_reader=read_agent_default)
    build_state_slot(child, request)

    if request.timeout_s > 0:
        _arm_watchdog(request.timeout_s)

    try:
        emit_child_boot_timing(child, clients=context.clients, now=time.perf_counter())
    except BaseException as exc:
        write_crashed_result(result_path, exc, code_reached=False)
        deliver_envelope_telemetry(clients=context.clients)
        return
    try:
        run_code(request.code, payload)
    except BaseException as exc:
        write_crashed_result(result_path, exc, code_reached=payload.code_reached)
        deliver_envelope_telemetry(clients=context.clients)
        return
    finally:
        _take_result_state_update(child, payload, state_injected=request.state_raw is not None)

    try:
        write_result(Path(result_path), payload)
    except BaseException as write_exc:
        write_crashed_result(result_path, write_exc, code_reached=payload.code_reached)

    deliver_run_telemetry(result_path, payload, clients=context.clients)


def main() -> None:
    boot_started_at = time.perf_counter()
    request_path = os.environ.get("MEIVA_EXEC_REQUEST_FILE")
    result_path = os.environ.get("MEIVA_EXEC_RESULT_FILE")
    if not request_path or not result_path:
        sys.stderr.write(
            "agent.execution.child needs MEIVA_EXEC_REQUEST_FILE and MEIVA_EXEC_RESULT_FILE "
            "in the environment — spawn it via agent.graph.exec._subprocess\n"
        )
        raise SystemExit(2)
    clients: ClientSet | None = None
    apply_oom_preference(os.environ, OOM_SCORE_ADJ_PATH)
    try:
        line_buffered_output()
        _install_signal_handlers()
        from base.cluster.machine.configured import ConfiguredMachineIdentity
        from base.config import ConfigBoot
        from meiva.boot import derived_clients

        config = ConfigBoot()
        config.boot()
        machine = ConfiguredMachineIdentity(config)
        clients = derived_clients(config, machine)
        context = MeivaContext(clients=clients, machine=machine)
        run_child(request_path, result_path, boot_started_at, config=config, context=context)
    except BaseException as exc:
        write_crashed_result(result_path, exc)

        deliver_envelope_telemetry(clients=clients)
    finally:
        slot = sys.modules.get("meiva.sdk_surface.process_context")
        context = None if slot is None else slot.unbind_context()
        if context is not None and context.clients is not clients:
            context.clients.close()
        if clients is not None:
            clients.close()


def write_crashed_result(
    result_path: str, exc: BaseException, *, code_reached: bool | None = False
) -> None:
    exc_type = type(exc).__name__
    exc_msg = str(exc)[:2000]
    full_traceback = _format_current_traceback(exc)
    envelope: dict[str, object] = {
        "v": 1,
        "kind": "crashed",
        "lifecycle_type": None,
        "exc_type": exc_type,
        "exc_msg": exc_msg,
        "full_traceback": full_traceback,
        "code_reached": code_reached,
        "state_update_error": None,
        "sdk_calls": None,
    }
    try:
        from agent.execution.protocol import ResultPayload as RuntimeResultPayload
        from agent.execution.protocol import write_result as runtime_write_result

        runtime_write_result(
            Path(result_path),
            RuntimeResultPayload(
                kind="crashed",
                exc_type=exc_type,
                exc_msg=exc_msg,
                full_traceback=full_traceback,
                code_reached=code_reached,
            ),
        )
        return
    except BaseException:
        try:
            write_stdlib_envelope(result_path, envelope)
        finally:
            logger.opt(exception=True).warning(
                "exec child: the result envelope writer failed; fell back to the stdlib writer"
            )


def write_stdlib_envelope(result_path: str, envelope: dict[str, object]) -> None:
    path = Path(result_path)
    fd: int | None = None
    try:
        data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        result_file = os.fdopen(fd, "wb")
        fd = None
        with result_file:
            result_file.write(data)
    except BaseException as write_exc:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            path.unlink()

        with contextlib.suppress(OSError, ValueError):
            sys.stderr.write(
                "exec child could not write crash result envelope: "
                f"{type(write_exc).__name__}: {write_exc}\n"
            )
            sys.stderr.flush()
        logger.opt(exception=True).warning(
            "exec child: the stdlib crash envelope write failed; the parent sees no result envelope"
        )


def _format_current_traceback(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


# meiva:note
# Blocks on the host's end of the control pipe for the life of the process. The host spawns
# this process with a pipe on stdin and keeps the write end until it has reaped it, so
# end-of-file means the host is gone (or is closing a reaped child) and this process group
# dies; a process that is not a group leader kills only itself, never the host's group. The
# thread never stops or joins: its only exit is the kill, and the interpreter does not wait
# for a daemon thread.
class _HostDeathWatch(threading.Thread):
    def __init__(self, control: int) -> None:
        super().__init__(name="exec-host-death-watch", daemon=True)
        self._control = control

    def run(self) -> None:
        with contextlib.suppress(OSError):
            while os.read(self._control, 4096):
                pass
        if os.getpgrp() == os.getpid():
            os.killpg(os.getpgrp(), signal.SIGKILL)
        os.kill(os.getpid(), signal.SIGKILL)


# meiva:note
# Arm the host-death watch before anything else. User code keeps /dev/null as stdin; the
# control pipe moves to a duplicate that only the watch reads. The group is this process's
# own session group, so its descendants die with it unless they left the group.
def die_with_host() -> None:
    control = os.dup(0)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    _HostDeathWatch(control).start()


def run_as_entry() -> None:
    die_with_host()
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()


if __name__ == "__main__":
    run_as_entry()
