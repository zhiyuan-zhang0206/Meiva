"""Child process for the SDK import and boot tests: each scenario needs a fresh interpreter.

Usage: `child_sdk_probes.py <scenario> [arg]`. Imports live inside the scenarios because the
order of the environment edits against the first `import meiva` is part of what is tested.
"""

import sys


def config_lite() -> None:
    import base.config as c
    import meiva  # noqa: F401  # pyright: ignore[reportUnusedImport]

    st = c.boot_state()  # pyright: ignore[reportAttributeAccessIssue]
    print(
        "MEIVA",
        st["mode"],
        st["upgrades"],
        "pydantic_settings" in sys.modules,
        "base.config.base" in sys.modules,
    )


def no_context_for_bare_script() -> None:
    import meiva

    print(hasattr(meiva, "context"))


def telemetry_overflow() -> None:
    import threading
    from datetime import UTC, datetime

    import meiva  # noqa: F401  # pyright: ignore[reportUnusedImport]
    from base import telemetry

    entered = threading.Event()
    release = threading.Event()

    def write(events: object) -> None:
        entered.set()
        assert release.wait(5)

    pipe = telemetry.EventPipeline(writer=write, batch_size=1, queue_maxsize=1)  # pyright: ignore[reportArgumentType]
    event = telemetry.Event(
        ts=datetime.now(UTC),
        trace_id=None,
        span_id=None,
        agent_id=None,
        machine="test",
        cluster="test",
        process="standalone",
        category="telemetry",
        event_name="sdk_call",
        level="info",
        source="system",
        target_agent_id=None,
    )
    try:
        pipe.enqueue(event)
        assert entered.wait(5)
        pipe.enqueue(event)
        pipe.enqueue(event)
    finally:
        release.set()
        assert pipe.stop(timeout=5).status is telemetry.DrainStatus.COMPLETED


def plain_import_sdk_events(target: str) -> None:
    import json
    from dataclasses import replace

    import meiva
    from base.agents.context.clients import ClientSet
    from base.telemetry.tests.capture.event_capture import capturing_events
    from meiva.sdk_surface import process_context

    with capturing_events() as events:
        context = meiva.context
        process_context.bind_context(
            replace(context, clients=ClientSet(pipeline_factory=events.producer))
        )
        assert meiva.files.read(target) == "hello"
        rows = [
            {"agent_id": event.agent_id, "machine": event.machine, "attributes": event.attributes}
            for event in events.named("sdk_call")
        ]
        assert rows[0]["machine"] == context.require_machine().name()
    print(json.dumps(rows))


def agent_profile_owner_url_guard() -> None:
    from base.config import settings

    try:
        _ = settings.data_plane.db_url
    except Exception as exc:
        print(
            type(exc).__name__,
            "agent-profile processes must receive a runner-class" in str(exc),
        )


def non_agent_import_unbound_context() -> None:
    import importlib
    import os

    os.environ.pop("MEIVA_AGENT_ID", None)
    import meiva
    from meiva.sdk_surface import install, installation, process_context

    assert getattr(meiva, "context", None) is None
    assert installation.installed() is None
    from base.agents.context import MeivaContext
    from base.agents.context.clients import ClientSet

    manual = MeivaContext(clients=ClientSet())
    process_context.bind_context(manual)
    importlib.reload(meiva)
    assert meiva.context is manual
    assert installation.installed() is None
    process_context.unbind_context()
    process_context.bind_host_process()
    from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner
    from meiva.sdk_surface.tests.installation_holder import held_installation

    with held_installation(installation.Installation.bare(sampling=fixed_sampling_owner())):
        meiva.ensure_plugins_loaded()
    assert process_context.is_host_process()
    assert getattr(meiva, "context", None) is None
    error = ValueError("retained explicit load failure")
    install.mark_load_failed(error)
    importlib.reload(meiva)
    assert process_context.is_host_process()
    assert getattr(meiva, "context", None) is None
    try:
        meiva.ensure_plugins_loaded()
    except ValueError as caught:
        assert caught is error
    else:
        raise AssertionError("explicit loading hid its prior failure")


# meiva:note
# What the launch environment makes of the process: the context, its loop flag, the child test.
def launched_identity() -> None:
    import json

    import meiva
    from base.agents.context import MeivaContext
    from base.agents.context.identity import AgentIdentity
    from meiva.sdk_surface import agent_identity

    ambient_id = agent_identity.agent_id()
    context = getattr(meiva, "context", None)
    identity = None if context is None else context.identity
    turn = MeivaContext(identity=AgentIdentity(5, True))
    outcome: dict[str, object] = {
        "agent_id": ambient_id,
        "owns_loop": None if identity is None else identity.owns_loop,
        "launched_child": agent_identity.is_launched_child(),
        "actor": agent_identity.require_actor() if ambient_id is not None else None,
        "turn_agent_id": agent_identity.require_agent_id(turn),
    }
    try:
        agent_identity.assert_self_action("restart")
    except RuntimeError as exc:
        outcome["self_action"] = str(exc)
    print(json.dumps(outcome))


# meiva:note
# The context a launched script builds from a lite configuration carries the bus as a factory: the
# build itself loads no bus, redis, psycopg or full-configuration module.
def launched_context_stays_cold() -> None:
    import json

    from base.config import ConfigBoot
    from meiva import boot

    context = boot.launched_context(env={"MEIVA_AGENT_ID": "7"}, config=ConfigBoot())
    assert context is not None
    heavy = ("base.events.live.bus", "psycopg", "pydantic_settings", "redis")
    print(
        json.dumps(
            {
                "eager_bus": context.bus is not None,
                "bus_factory": context.bus_factory is not None,
                "loaded": sorted(m for m in heavy if m in sys.modules),
            }
        )
    )


# meiva:note
# A process nobody launched as an agent: reads fail fast and no context appears.
def unestablished_identity() -> None:
    import json

    import meiva
    from meiva.sdk_surface import agent_identity

    outcome: dict[str, object] = {"agent_id": agent_identity.agent_id()}
    for name, call in (
        ("require_agent_id", agent_identity.require_agent_id),
        ("require_actor", agent_identity.require_actor),
        ("assert_self_action", lambda: agent_identity.assert_self_action("terminate")),
        ("context", lambda: meiva.context),
    ):
        try:
            call()
        except (RuntimeError, AttributeError) as exc:
            outcome[name] = str(exc)
    outcome["launched_child"] = agent_identity.is_launched_child()
    outcome["has_context"] = getattr(meiva, "context", None) is not None
    print(json.dumps(outcome))


# meiva:note
# The external tool profile precedes a local context's agent id, but not an explicit one. The
# profile is read from the environment once, by the plugin load that builds the installation.
def external_profile_actor() -> None:
    import json

    from base.agents.context import MeivaContext
    from base.agents.context.identity import AgentIdentity
    from meiva import boot
    from meiva.sdk_surface import agent_identity, process_context

    process_context.bind_context(MeivaContext(identity=AgentIdentity(405, True)))
    boot.ensure_plugins_loaded()
    print(
        json.dumps(
            {
                "local": agent_identity.require_actor(),
                "explicit": agent_identity.require_actor(
                    MeivaContext(identity=AgentIdentity(406, True))
                ),
            }
        )
    )


# meiva:note
# A profile the SDK cannot parse is a fault in who is calling: the plugin load that reads it
# fails and leaves no installation, so no SDK call is ever admitted under a guessed identity.
def unreadable_external_profile() -> None:
    import json

    from meiva import boot
    from meiva.sdk_surface import installation

    try:
        boot.ensure_plugins_loaded()
    except ValueError as exc:
        print(json.dumps({"rejected": True, "installed": installation.installed() is not None}))
        print(type(exc).__name__)
    else:
        print(json.dumps({"rejected": False, "installed": installation.installed() is not None}))


SCENARIOS = {
    "config_lite": config_lite,
    "no_context_for_bare_script": no_context_for_bare_script,
    "telemetry_overflow": telemetry_overflow,
    "plain_import_sdk_events": plain_import_sdk_events,
    "agent_profile_owner_url_guard": agent_profile_owner_url_guard,
    "non_agent_import_unbound_context": non_agent_import_unbound_context,
    "launched_identity": launched_identity,
    "launched_context_stays_cold": launched_context_stays_cold,
    "unestablished_identity": unestablished_identity,
    "external_profile_actor": external_profile_actor,
    "unreadable_external_profile": unreadable_external_profile,
}

SCENARIOS[sys.argv[1]](*sys.argv[2:])  # pyright: ignore[reportCallIssue]
