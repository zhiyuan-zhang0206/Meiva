"""The standard `/healthz` daemon: one declaration, everything else derived.

Most roster entries are the same shape: a Python daemon started with
``python -m <module>`` that serves the shared Meiva ``/healthz`` on its slot of the
health-port table and records a pidfile. For that shape the only decisions are the
service name, the entry module, the capability set and whether it uses the
database; ``healthz_daemon`` derives the rest (launch command, probe URL, identity
probe, health name) so the same fact is never typed twice. Entries with their own
protocol (gate, gateway, frontend, browser, the native backends) keep writing a
``ServiceSpec`` out in full — their probes are not derivable.

Nothing here reads configuration at import: the URL and the pidfile are resolved
when ``build_services()`` runs, like every other probe target on the roster.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import partial
from pathlib import Path

from pydantic import BaseModel

from base.cluster.machine import MachineRole
from base.daemon.endpoints import ServiceEndpoints
from base.daemon.health import DaemonProbe, probe_daemon
from base.host.env.health_ports import DEFAULT_PORTS
from services.supervision.roster.service_spec import DbAccess, ServiceSpec

# `meiva-root` renders this prefix into `cd <repo> && exec <cmd>`; a command that stays
# bare tokens keeps the unit's pid a direct child of root (`meiva_root_glue.manifests`).
_PYTHON_M = ".venv/bin/python -m"

_SESSION_KEBAB = r"[a-z][a-z0-9]*(-[a-z0-9]+)*"
_DOTTED_MODULE = r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*"


def health_name_of(session: str) -> str:
    """The health name of a standard daemon: its kebab session with underscores.

    This is the key of its port slot, the ``name`` its ``/healthz`` answers with
    the stem of its ``MEIVA_<NAME>_HEALTH_PORT`` setting and of its ``run/<name>.pid`` pidfile.

    Raises:
        ValueError: ``session`` is not lowercase kebab-case.
    """
    if re.fullmatch(_SESSION_KEBAB, session) is None:
        raise ValueError(f"service session {session!r} must be lowercase kebab-case")
    return session.replace("-", "_")


# meiva:note
# The ``/healthz`` URL of daemon ``name``, from the health-port table the root built. The row carries
# the port, so nothing here reads a setting or a home.
def healthz_url(name: str, endpoints: ServiceEndpoints) -> str:
    return f"http://localhost:{endpoints.of(name).health_port}/healthz"


# meiva:note
# The probe is bound to the unit home the roster hands in, the same home
# `probe_gate` and `probe_protocol` are bound to: a health identity check is worth
# nothing if the value it compares against is the one the probing process happens
# to run in.
#
# Also bound: the daemon's name, its `/healthz` URL and the pidfile this unit recorded for it, all
# three taken from the row of the table the root built. Public because plugin-registered services
# declare their own specs and must state the same contract without restating the probe
# (`meiva_builtins/plugins/*/services.py`).
def daemon_identity(
    name: str, pidfile: Path, *, home: Path, endpoints: ServiceEndpoints
) -> Callable[[], DaemonProbe]:
    return partial(
        probe_daemon, name, healthz_url(name, endpoints), pidfile=pidfile, expected_home=home
    )


# meiva:note
# `endpoints` is the health-port table the root built from its own config: this daemon's row there
# supplies both its `/healthz` URL and its pidfile, so the factory reads no setting and no home.
def healthz_daemon(
    session: str,
    *,
    module: str,
    home: Path,
    endpoints: ServiceEndpoints,
    capabilities: frozenset[MachineRole],
    requires_db: bool,
    gate: Callable[[], str | None] | None = None,
    profile: str | None = None,
    no_profile_marker: bool = False,
    config_inputs: tuple[Path, ...] = (),
    plugin_config: tuple[str, BaseModel] | None = None,
    db_access: DbAccess | None = None,
    stop_ceiling_s: float | None = None,
) -> ServiceSpec:
    """A roster entry for a standard ``/healthz`` daemon.

    Args:
        session: lowercase kebab service name, the unit id (``delivery-watchdog``).
        module: the entry module ``python -m`` runs. Written out, never guessed from
            the session: four daemons live in a package of another name.
        capabilities: which machine capabilities run it.
        requires_db: whether it dials Postgres; no default because a database
            outage holds exactly the services that say yes.
        gate, profile, no_profile_marker, config_inputs, plugin_config, db_access, stop_ceiling_s: the optional
            declarations of ``ServiceSpec``, passed through unchanged.

    Raises:
        ValueError: ``session`` or ``module`` is malformed, or the health name has
            no slot in the health-port table (``DEFAULT_PORTS``).
    """
    if re.fullmatch(_DOTTED_MODULE, module) is None:
        raise ValueError(f"service {session!r}: module {module!r} is not a dotted module path")
    name = health_name_of(session)
    if name not in DEFAULT_PORTS:
        raise ValueError(
            f"service {session!r}: health name {name!r} has no port slot "
            f"(known: {sorted(DEFAULT_PORTS)})"
        )
    endpoint = endpoints.of(name)
    return ServiceSpec(
        session=session,
        cmd=f"{_PYTHON_M} {module}",
        capabilities=capabilities,
        requires_db=requires_db,
        curl_url=healthz_url(name, endpoints),
        identity_probe=daemon_identity(name, endpoint.pidfile, home=home, endpoints=endpoints),
        health_name=name,
        gate=gate,
        profile=profile,
        no_profile_marker=no_profile_marker,
        config_inputs=config_inputs,
        plugin_config=plugin_config,
        db_access=db_access,
        stop_ceiling_s=stop_ceiling_s,
    )
