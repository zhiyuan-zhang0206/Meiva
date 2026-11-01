import threading
from contextlib import closing
from functools import partial

from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.clock import Clock
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentityReader
from base.config.tests.configuration import snapshot_process_config
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process.code_version import CodeVersion
from base.paths import meiva_home
from base.telemetry import process_name
from meiva.boot import clients_over
from meiva.sdk_surface import process_context
from services.wake.schedule_manager import engine
from services.wake.schedule_manager.stall_policy import StallPolicy

# meiva:note
# Today's production budget: a test that needs a stall passes a short one.
UNHURRIED = StallPolicy(timeout_s=1200.0)


# meiva:note
# Run the schedule in this process the way `runner.run` does: bind the actor, then the engine.
def run_schedule(
    schedule_id: int,
    revision: int | None = None,
    *,
    machine: MachineIdentityReader,
    policy: StallPolicy = UNHURRIED,
) -> int:
    image = process_context.loaded_code_image()
    version = CodeVersion(image)
    config = snapshot_process_config()
    gate = ProcessDbGate(process=process_name(), version=version.get)
    database = partial(
        Database.from_settings,
        gate=gate,
        direct_url=ClusterDirectUrl(machine.host, home=meiva_home()),
    )
    with closing(clients_over(config, machine, database)) as clients:
        process_context.bind_context(
            MeivaContext(
                identity=AgentIdentity(
                    agent_id=None, owns_loop=True, actor=f"schedule:{schedule_id}"
                ),
                clients=clients,
                machine=machine,
                clock_factory=Clock.from_settings,
                meiva_home=meiva_home(),
                environ=config.process_environment(),
            )
        )
        return engine.run(
            schedule_id,
            revision,
            engine.RunnerRoot(
                database=database(),
                inputs=ScheduleInputs(
                    database, clients.event_pipeline, image, machine=machine, home=meiva_home()
                ),
                plugins=engine.PluginInputs(config=config, machine=machine, clients=clients),
                policy=policy,
                home=meiva_home(),
                main_thread_id=threading.get_ident(),
            ),
        )
