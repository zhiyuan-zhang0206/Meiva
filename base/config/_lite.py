"""Boot-lite configuration owned by a ConfigBoot instance.

Owners prepare at their process boundary and build the eager chain on first
non-lite use. Each owns its lock, pending overlays and stable view. The existing
module facade remains a temporary adapter during complete consumer migration.
Lite parsing, environment delivery and the bounded/re-entrant full-build gate
retain their previous timing.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Generator, Mapping, MutableMapping
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from threading import RLock, get_ident
from typing import Any, Protocol

from base.config.config_registry import DOMAIN_ATTRS
from base.config.lite_parse import parse_field
from base.config.profiles import PROCESS_PROFILES, profile_domain_error
from base.host.env.boot_actions import apply_process_timezone
from base.host.env.boot_policy import BootstrapFetch
from base.host.env.bootstrap import (
    CONFIG_FETCH_ENV,
    CONFIG_FETCH_SKIP,
    LaunchFacts,
    config_source_is_local,
    inject_config_from_gateway,
    launch_facts,
    should_fetch_from_gateway,
)
from base.host.env.config_lite_table import (
    FIELD_ALIASES,
    FIELD_DOMAINS,
    LITE_FIELDS,
    REQUIRED_FIELDS,
)
from base.host.env.dotenv_boot import (
    PLACEHOLDER_DB_URL,
    EnvBootResult,
    load_meiva_env,
    read_database_delivery,
)
from base.host.env.process_profile import (
    MEIVA_PROCESS_PROFILE_ENV,
    ProcessProfile,
    parse_process_profile,
)
from base.paths.home import default_home, home_from_env, resolve_meiva_home

# `MEIVA_CONFIG_BOOT=eager` — the operator's instant rollback to the eager boot.
BOOT_MODE_ENV = "MEIVA_CONFIG_BOOT"
BOOT_MODE_EAGER = "eager"

_LITE = "lite"
_FULL = "full"

# Internal invariant (task #3696 exception inventory): bound for a read
# waiting on another thread's in-flight eager build. Not config: the wait runs
# before the config chain exists (reading a field would itself trigger the
# upgrade), and the value is fixed at 3x the bootstrap fetch bound
# (base/host/env/bootstrap.py _FETCH_TIMEOUT_S = 10s) -- the slowest legitimate
# segment of a build. A slower build degrades to the retryable
# ConfigBuildWaitTimeoutError, so no operator knob is warranted.
BUILD_WAIT_TIMEOUT_SECONDS = 30.0

# The one upgrade lock: prepare, the build, and the install run under it.


# The never-dialed placeholder for the required redis URL (mirrors the eager
# boot path); the db placeholder is PLACEHOLDER_DB_URL, which base/db
# refuses with an actionable error.
_LITE_REDIS_URL = "redis://config-lite@127.0.0.1:1/0"


class EagerSettings(Protocol):
    @property
    def profile(self) -> str | None: ...

    env_boot: EnvBootResult

    def has_domain(self, name: str) -> bool: ...


class _InstalledBundle(Protocol):
    @property
    def settings(self) -> EagerSettings: ...

    @property
    def exports(self) -> Mapping[str, object]: ...


def _default_value(owner: ConfigBoot, name: str, default_kind: str, literal: object) -> object:
    """The field's default, per the named default kind recorded in the table."""
    if default_kind == "literal":
        return literal
    if default_kind == "none":
        return None
    if default_kind == "factory_empty_list":
        return []
    if default_kind == "otel_endpoint_from_port":
        # Mirrors ObservabilitySettings._default_local_otlp_endpoint: when the
        # endpoint itself is not explicitly set, it follows the port.
        return f"http://127.0.0.1:{owner.resolve('telemetry_otlp_port')}"
    raise ValueError(
        f"unknown default kind {default_kind!r} for config field {name!r}"
    )  # pragma: no cover


class _DomainView:
    """Read proxy for one config domain while the process is lite.

    A field listed in the static index for this domain resolves in place; any
    other attribute is the eager sub-model's business and upgrades. After an
    upgrade the proxy keeps working: it delegates to the real sub-model."""

    __slots__ = ("_domain", "_owner")

    _owner: ConfigBoot
    _domain: str

    def __init__(self, owner: ConfigBoot, domain: str) -> None:
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_domain", domain)

    def __getattr__(self, name: str) -> object:
        domain = self._domain
        if self._owner.mode == _FULL:
            return getattr(getattr(self._owner._current_settings(), domain), name)
        if name in self._owner.pending and FIELD_DOMAINS.get(name) == domain:
            return self._owner.pending[name]
        row = LITE_FIELDS.get(name)
        if row is not None and row[0] == domain:
            return self._owner.resolve(name)
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        if not self._owner._maybe_upgrade(f"settings.{domain}.{name}"):
            raise AttributeError(build_window_message(f"settings.{domain}.{name}"))
        return getattr(getattr(self._owner._current_settings(), domain), name)

    def __setattr__(self, name: str, value: object) -> None:
        domain = object.__getattribute__(self, "_domain")
        if self._owner.mode != _FULL:
            self._owner.upgrade(f"settings.{domain}.{name} assignment")
        setattr(getattr(self._owner._current_settings(), domain), name, value)


class _SettingsView:
    """The stable `settings` object: lite until the first eager touch, then the
    constructed singleton. The object identity never changes, so every
    `from base.config import settings` binding — and every `set_field`
    write — keeps working across the upgrade."""

    __slots__ = ("_domains", "_owner")

    _owner: ConfigBoot
    _domains: dict[str, _DomainView]

    def __init__(self, owner: ConfigBoot) -> None:
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_domains", {})

    @property
    def profile(self) -> str | None:
        if self._owner.mode == _FULL:
            return self._owner._current_settings().profile
        self._owner.prepare()
        return self._owner.profile

    def has_domain(self, name: str) -> bool:
        """Whether this process's profile constructs the `name` config domain.

        The escape hatch for dynamic code (plugins) that must probe before
        reading; static code should simply access `settings.<domain>` and let
        the fail-fast AttributeError point at the fix.
        """
        if self._owner.mode == _FULL:
            return bool(self._owner._current_settings().has_domain(name))
        self._owner.prepare()
        profile = self._owner.profile
        if profile is None:
            return True
        return name in PROCESS_PROFILES[profile]

    def __getattr__(self, name: str) -> Any:
        if self._owner.mode == _FULL:
            return getattr(self._owner._current_settings(), name)
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        if name in DOMAIN_ATTRS:
            self._owner._check_domain_allowed(name)
            domains: dict[str, _DomainView] = object.__getattribute__(self, "_domains")
            view = domains.get(name)
            if view is None:
                view = domains[name] = _DomainView(self._owner, name)
            return view
        if not self._owner._maybe_upgrade(f"settings attribute {name!r}"):
            raise AttributeError(build_window_message(f"settings.{name}"))
        return getattr(self._owner._current_settings(), name)

    def __setattr__(self, name: str, value: object) -> None:
        if self._owner.mode != _FULL:
            self._owner.upgrade(f"settings.{name} assignment")
        setattr(self._owner._current_settings(), name, value)


def build_window_message(target: str) -> str:
    return f"config boot-lite: {target} is not readable while the eager config build is in flight"


class ConfigBuildWaitTimeoutError(AttributeError):
    """A read waited out the bounded window for another thread's eager build.

    Retryable: the other thread's build keeps running (or a fresh attempt runs
    after a failed one), and nothing is corrupted by the wait itself. Inherits
    AttributeError so attribute-read callers keep their previous catch shape
    for window conditions.
    """


def _build_wait_timeout_message(
    target: str, timeout_seconds: float = BUILD_WAIT_TIMEOUT_SECONDS
) -> str:
    return (
        f"config boot-lite: {target} is not readable yet: the in-flight eager config "
        f"build exceeded the {timeout_seconds:g}s wait bound; retry shortly"
    )


def _require_local_fields(env: Mapping[str, str]) -> None:
    """W1: a local-source unit fails fast when a required field has no value.

    A field with no default (the data-plane URLs) is filled from the unit's
    `.env` or the process env; with neither, the eager boot raised a pydantic
    "Field required" at construction. The boot-lite boot must keep that
    fail-fast at the same point — the check is local-branch-only by design: a
    runner's values arrive from the gateway fetch (validated after it), and
    skip/bare processes get placeholders.
    """
    missing = [name for name in sorted(REQUIRED_FIELDS) if env.get(FIELD_ALIASES[name]) is None]
    if missing:
        named = ", ".join(f"{FIELD_ALIASES[name]} ({name})" for name in missing)
        raise ValueError(
            f"required config missing for this local-source unit: {named} — its .env is the "
            f"config source (config_source_is_local()) and no value is present in the "
            f"environment. Set it in $MEIVA_HOME/.env or the process environment."
        )


def _plant_placeholders(env: dict[str, str] | os._Environ[str]) -> None:
    """Plant never-dialed placeholders for the required data-plane URLs so a
    construction succeeds without a gateway fetch. A value already in env/.env
    is left alone — the placeholder only fills the nothing-at-all case."""
    env.setdefault("MEIVA_DB_URL", PLACEHOLDER_DB_URL)
    env.setdefault("MEIVA_REDIS_URL", _LITE_REDIS_URL)


def _log_upgrade(reason: str) -> None:
    # Debug-only observability (Q6): the event is already visible in
    # `boot_state()` for tests and acceptance runs.
    if "loguru" not in sys.modules:
        return
    from base.log import logger

    logger.debug("config boot-lite upgraded to the eager config chain (reason={!r})", reason)


# meiva:note
# One composition owner's lazy configuration, overlays and build gate.
#
# Construction performs no environment delivery or full-model build. Call boot() at the existing
# process entry, the composition root. The root reads `view` once and builds each component's
# frozen `XxxConfig` value from it; `view` itself, and closures over it, stay in the root module
# and are never handed to a component. A component that must see a later change is handed the
# `ConfigBoot` as a real resource and builds a fresh value at its unit of work. A view always
# resolves this instance, including after upgrade.
# meiva:link base/config/docs/config-roots.meiva.okf.md
class ConfigBoot:
    __slots__ = (
        "_lock",
        "env_boot",
        "environment",
        "mode",
        "pending",
        "prepared",
        "profile",
        "reason",
        "settings",
        "upgrades",
        "upgrading",
        "upgrading_thread",
        "view",
        "wait_timeout_seconds",
    )

    def __init__(
        self,
        *,
        environment: dict[str, str] | None = None,
        wait_timeout_seconds: float | None = None,
    ) -> None:
        self._lock = RLock()
        self.view = _SettingsView(self)
        self.env_boot: EnvBootResult | None = None
        self.environment: dict[str, str] | None = environment
        self.mode = _LITE
        self.reason: str | None = None
        self.upgrades = 0
        self.upgrading = False
        self.upgrading_thread: int | None = None
        self.prepared = False
        self.profile: ProcessProfile | None = None
        # Overlay writes made in lite mode, in insertion order: name -> raw value.
        self.pending: dict[str, Any] = {}
        # This owner's constructed model, set by _install().
        self.settings: Any = None
        self.wait_timeout_seconds: float | None = wait_timeout_seconds

    def _current_settings(self) -> EagerSettings:
        return self.settings

    # meiva:note
    # The launch environment this owner reads: the captured delivery of a read-only
    # attachment, else the live `os.environ` of a booting process.
    def process_environment(self) -> MutableMapping[str, str]:
        return os.environ if self.environment is None else self.environment

    # meiva:note
    # What this owner's launch environment says about the credential it presents to the
    # gateway (`GatewayAccess` takes it): the delivered machine token, the launcher profile
    # and whether this unit serves the gateway. Reads the home's `.env` as it is now.
    def launch_facts(self) -> LaunchFacts:
        from base.host.env.dotenv_file import env_file_path, read_env_aliases

        env = self.process_environment()
        home = home_from_env(env, user_home=default_home().parent)
        return launch_facts(env, env_file=read_env_aliases(env_file_path(home)))

    def _apply_cluster_timezone(self) -> None:
        name = self.get_field("timezone") if self.field_explicitly_set("timezone") else None
        apply_process_timezone(name)

    def read_process_environment(self) -> None:
        """Capture an existing startup delivery without delivering it again.

        Attachments own reads, overlays and connections, while process entry
        boot owns dotenv authority, bootstrap/provider delivery and process TZ.
        Both lite reads and later eager construction use this owner's values.
        """
        with self._lock:
            if self.prepared:
                if self.environment is not None:
                    return
                raise RuntimeError("configuration owner was already prepared")
            if os.environ.get("MEIVA_HARNESS_HANDLE"):
                for key in [k for k, v in os.environ.items() if k.startswith("MEIVA_") and v == ""]:
                    os.environ.pop(key, None)
                if not os.environ.get("MEIVA_DB_URL"):
                    load_meiva_env(os.environ, home=resolve_meiva_home())
            environment = dict(os.environ)
            profile = environment.get(MEIVA_PROCESS_PROFILE_ENV)
            parsed_profile = None if profile is None else parse_process_profile(profile)
            for _name, row in LITE_FIELDS.items():
                raw = environment.get(row[1])
                if raw is not None:
                    parse_field(_name, row[1], row[2], row[5], raw)
            self.environment = environment
            self.profile = parsed_profile
            self.env_boot = read_database_delivery(
                environment, home=home_from_env(environment, user_home=default_home().parent)
            )
            self.prepared = True

    def complete_read_model(self) -> EagerSettings:
        """Build the SDK's complete read surface from this same delivery."""
        from base.config._full import Settings, build

        if self.environment is None or self.env_boot is None:
            return Settings(profile=None)
        return build(env_boot=self.env_boot, environment=self.environment).settings

    def refresh_data_plane_settings(self) -> None:
        """Refresh only this owner's data-plane slice at the existing call point."""
        if self.environment is not None:
            raise RuntimeError("a read-only configuration owner cannot redeliver the data plane")
        from base.config._full import Settings, refresh_data_plane_settings

        self.upgrade("refresh_data_plane_settings()")
        current = self._current_settings()
        if isinstance(current, Settings):
            refresh_data_plane_settings(current)

    def resolve(self, name: str) -> object:
        """Resolve one `LITE_FIELDS` entry: pending override > env > default."""
        self.prepare()
        if name in self.pending:
            return self.pending[name]
        _domain, alias, kind, default_kind, literal, check = LITE_FIELDS[name]
        raw = (os.environ if self.environment is None else self.environment).get(alias)
        if raw is not None:
            return parse_field(name, alias, kind, check, raw)
        return _default_value(self, name, default_kind, literal)

    def field_explicitly_set(self, name: str) -> bool:
        """Whether `name` was explicitly provided to this process rather than
        defaulted — the lite equivalent of `model_fields_set` for a boot-built
        singleton: its env alias is present (the `.env` load ran), or an overlay
        pinned it (`set_field`; pydantic records a post-init setattr in
        `model_fields_set` too, so both modes agree). `cluster_tz_name` is built
        on this: the field default alone does not make a value authoritative."""
        self.prepare()
        if self.mode == _FULL:
            return name in getattr(self._current_settings(), FIELD_DOMAINS[name]).model_fields_set
        environment = os.environ if self.environment is None else self.environment
        return name in self.pending or environment.get(FIELD_ALIASES[name]) is not None

    def _check_domain_allowed(self, domain: str) -> None:
        """The lite half of the profile fail-fast (the eager half lives in
        `Settings.__getattr__`; both raise the same message)."""
        profile = self.profile
        if profile is not None and domain not in PROCESS_PROFILES[profile]:
            raise profile_domain_error(profile, domain)

    def get_field(self, name: str) -> Any:
        """Current value of a leaf field by name, resolved to its owning sub-model.

        The escape hatch for reflective / dynamic access — a flat
        `getattr(settings, name)` no longer works now that fields live on
        `settings.<domain>`. Static access should use the nested attribute directly
        (`settings.lm.llm_state.model`); this is for call sites that hold the field name as a
        runtime string (health-port map, model-key map, capability probes)."""
        domain = FIELD_DOMAINS[name]
        if self.mode == _FULL:
            return getattr(getattr(self._current_settings(), domain), name)
        self._check_domain_allowed(domain)
        if name in self.pending:
            return self.pending[name]
        if name in LITE_FIELDS:
            return self.resolve(name)
        if self.upgrading and self.upgrading_thread != get_ident():
            self._wait_for_in_flight_build(f"get_field({name!r})")
        self.upgrade(f"get_field({name!r})")
        return getattr(getattr(self._current_settings(), domain), name)

    def set_field(self, name: str, value: object) -> None:
        """Set a field on this owner; existing views/readers see the same model.

        Used by the per-agent config overlay at process boot.

        In lite mode ANY registered field is recorded as a pending override and
        replayed onto the constructed singleton at upgrade; reads see it already
        (pending resolves ahead of the index), so an overlay write never forces the
        eager chain — the hosted exec child writes the agent's whole frozen set,
        most of which the boot-path index does not carry (#3621 BLK-1). An unknown
        name is a KeyError, and a domain outside the profile fails fast, both as in
        the eager path."""
        domain = FIELD_DOMAINS[name]
        if self.mode == _FULL:
            setattr(getattr(self._current_settings(), domain), name, value)
            return
        self._check_domain_allowed(domain)
        self.pending[name] = value

    def prepare(self) -> None:
        """Make the boot-lite state resolvable — once per process.

        Runs at the `base.config` import tail for every non-skip process (the
        settings-lite maintenance verbs keep their deferred load) and at the first
        config read for a skip process. Mirrors the eager boot's env work exactly:

        1. `load_meiva_env()` — the `.env` load + the env-authority force/drop pass;
        2. the config-source decision: a local unit must hold its required
           data-plane values (W1 check), a configured pure runner fetches
           `GET /api/bootstrap` (the same import-time fetch, and the same
           BootstrapFetchError when the gateway is unreachable), everything else
           gets the never-dialed placeholders so a later construction succeeds;
        3. every boot-path field that is present in the environment parses and
           validates now — the import-time fail-fast for `LITE_FIELDS`;
        4. the cluster clock is applied (`apply_cluster_timezone`).
        """
        if self.prepared:
            return
        with self._lock:
            if self.prepared:
                return
            # Latched first: a re-entrant read during step 4 (cluster_tz_name reads
            # through the view) must not prepare again.
            self.prepared = True
            target_env = os.environ if self.environment is None else self.environment
            home = (
                resolve_meiva_home()
                if self.environment is None or "MEIVA_HOME" not in self.environment
                else Path(self.environment["MEIVA_HOME"])
            )
            self.env_boot = load_meiva_env(target_env, home=home)
            profile = target_env.get(MEIVA_PROCESS_PROFILE_ENV)
            self.profile = None if profile is None else parse_process_profile(profile)
            self._apply_source_decision(home, target_env)
            for name, row in LITE_FIELDS.items():
                raw = target_env.get(row[1])
                if raw is not None:
                    parse_field(name, row[1], row[2], row[5], raw)
            self._apply_cluster_timezone()

    def _apply_source_decision(
        self, home: Path, target_env: dict[str, str] | os._Environ[str]
    ) -> None:
        """The eager boot's source decision, minus the construction it guarded.

        `_plant_placeholders` keeps the eager boot's env side effect (the
        never-dialed URLs) so a later `Settings()` construction finds the same
        environment it would have found eagerly."""
        from base.host.env.dotenv_file import env_file_path, read_env_aliases

        env_file = read_env_aliases(env_file_path(home))
        if config_source_is_local(target_env, env_file=env_file):
            _require_local_fields(target_env)
        elif target_env.get(CONFIG_FETCH_ENV) == CONFIG_FETCH_SKIP:
            _plant_placeholders(target_env)
        elif should_fetch_from_gateway(target_env, env_file=env_file):
            self.env_boot = inject_config_from_gateway(
                target_env, home=home, now=time.time(), tuning=BootstrapFetch()
            )
        else:
            _plant_placeholders(target_env)

    # meiva:note
    # Hold the upgrade lock with this thread marked as the in-flight builder.
    @contextmanager
    def in_flight_build(self) -> Generator[None]:
        with self._lock:
            self.prepare()
            self.upgrading = True
            self.upgrading_thread = get_ident()
            try:
                yield
            finally:
                self.upgrading = False
                self.upgrading_thread = None

    def upgrade(self, reason: str) -> EagerSettings:
        """Build this owner's eager chain once.

        Under the upgrade lock: prepare if not yet prepared, build the full state
        (`_full.build()` — the sub-models, registry and `Settings` singleton from
        the prepared environment), install it on this owner, replay the pending
        overlay writes in insertion order, and latch `full`. Idempotent: every later
        call returns the constructed singleton. The reason (a short caller tag)
        lands in the debug log and `boot_state()`."""
        with self._lock:
            if self.mode == _FULL:
                return self.settings
            with self.in_flight_build():
                from base.config._full import build

                env_boot = self.env_boot
                if env_boot is None:
                    raise RuntimeError("config boot did not produce an environment delivery result")
                bundle = (
                    build(env_boot=env_boot)
                    if self.environment is None
                    else build(
                        env_boot=env_boot, environment=self.environment, profile=self.profile
                    )
                )
                self._install(bundle, reason)
                for name, value in tuple(self.pending.items()):
                    setattr(getattr(self.settings, FIELD_DOMAINS[name]), name, value)
                self.pending.clear()
        _log_upgrade(reason)
        return self.settings

    def _install(self, bundle: _InstalledBundle, reason: str) -> None:
        self.settings = bundle.settings
        self.mode = _FULL
        self.prepared = True
        self.reason = reason
        self.upgrades += 1

    def _wait_for_in_flight_build(self, reason: str) -> None:
        """Bounded wait for another thread's in-flight eager build.

        The builder holds `self._lock` across the whole upgrade (prepare, build,
        install, overlay replay), so acquiring it IS the wait. On acquisition the
        chain is installed and the caller can serve the full value; if the
        in-flight attempt died before installing, this thread runs the build
        itself so the real error surfaces. On expiry the read raises the
        retryable ConfigBuildWaitTimeoutError -- the other thread's build keeps running
        and installs when it finishes, so a retry is the right move."""
        timeout = (
            BUILD_WAIT_TIMEOUT_SECONDS
            if self.wait_timeout_seconds is None
            else self.wait_timeout_seconds
        )
        if not self._lock.acquire(timeout=timeout):
            raise ConfigBuildWaitTimeoutError(_build_wait_timeout_message(reason, timeout))
        try:
            if self.mode != _FULL:
                self.upgrade(reason)
        finally:
            self._lock.release()

    def _maybe_upgrade(self, reason: str) -> bool:
        """Upgrade unless THIS thread is the one already building.

        A read that arrives while another thread's build is in flight waits for it
        (bounded) and then serves the full chain. Only a read made by the building
        thread itself -- which must not wait on its own build -- returns False, and
        the caller serves the lite value or raises the documented window error."""
        if self.mode == _FULL:
            return True
        if self.upgrading:
            if self.upgrading_thread == get_ident():
                return False
            self._wait_for_in_flight_build(reason)
            return True
        self.upgrade(reason)
        return True

    def ensure_eager(self) -> EagerSettings:
        """Construct the full config chain now, at this point in the process.

        The explicit full-validation entry point for processes that must fail fast
        on every field at boot (the gateway, the ops daemons, the agent host — the
        callers whitelisted for #3621): `import base.config` alone no longer
        constructs every domain."""
        return self.upgrade("ensure_eager()")

    def boot(self) -> None:
        """The `base.config` import tail: prepare, or go eager now.

        `MEIVA_CONFIG_BOOT=eager` is the operator's instant rollback to the eager
        boot; any other explicit value is a typo and fails fast rather than
        silently booting lite.

        Precedence when both `MEIVA_CONFIG_FETCH=skip` and `MEIVA_CONFIG_BOOT=eager`
        are set: skip wins and the process stays boot-lite. This is deliberate.
        `cli.main` sets skip for the settings-lite maintenance verbs (stop, status,
        the config verbs) so they stay usable while the gateway is down or the
        `.env` is broken; the operator's rollback flag may sit in the same unit
        environment, and honouring it there would make the repair tool fail on
        exactly the fault it exists to repair (#3621 / 405 ruling). A caller that
        needs the eager chain regardless, such as the test session, calls
        `ensure_eager()` instead of relying on the flag."""
        target_env = os.environ if self.environment is None else self.environment
        if target_env.get("MEIVA_HARNESS_HANDLE"):
            for key in [k for k, v in target_env.items() if k.startswith("MEIVA_") and v == ""]:
                target_env.pop(key, None)
        mode = target_env.get(BOOT_MODE_ENV)
        if mode is not None and mode != BOOT_MODE_EAGER:
            raise ValueError(
                f"{BOOT_MODE_ENV}={mode!r} is not a known boot mode; must be {BOOT_MODE_EAGER!r} "
                f"or unset (the unset default is the lazy boot-lite chain)"
            )
        if target_env.get(CONFIG_FETCH_ENV) == CONFIG_FETCH_SKIP:
            # skip wins over eager: a settings-lite maintenance verb must stay
            # repairable (a broken .env must not be able to block the tool that
            # fixes it), even when the operator's rollback flag sits in the unit
            # environment (#3621 / 405 ruling).
            return
        if mode == BOOT_MODE_EAGER:
            self.upgrade(f"{BOOT_MODE_ENV}={BOOT_MODE_EAGER}")
        else:
            self.prepare()

    def is_full(self) -> bool:
        """Whether this process has upgraded to the eager config chain."""
        return self.mode == _FULL

    def boot_state(self) -> dict[str, Any]:
        """Private test/acceptance hook: the boot-lite state machine snapshot."""
        return {
            "mode": self.mode,
            "reason": self.reason,
            "upgrades": self.upgrades,
            "pending_count": len(self.pending),
            "prepared": self.prepared,
        }


class _LegacyConfigBoot(ConfigBoot):
    """Temporary adapter for the unmigrated facade, with its existing bindings.

    The facade (`base.config`) binds itself once its module body is complete
    (`bind_facade`): it hands over its namespace and its timezone applier, so this module
    never imports the package that imports it."""

    __slots__ = ("_apply_timezone", "_facade")

    def bind_facade(self, namespace: dict[str, Any], apply_timezone: Callable[[], None]) -> None:
        self._facade = namespace
        self._apply_timezone = apply_timezone

    def _current_settings(self) -> EagerSettings:
        return self._facade["settings"]

    def _apply_cluster_timezone(self) -> None:
        self._apply_timezone()

    def _install(self, bundle: _InstalledBundle, reason: str) -> None:
        self._facade.update(bundle.exports)
        self._facade["settings"] = bundle.settings
        super()._install(bundle, reason)


# The existing legacy owner remains until every consumer has an explicit input.
_state = _LegacyConfigBoot()


def resolve(name: str) -> object:
    return _state.resolve(name)


def field_explicitly_set(name: str) -> bool:
    return _state.field_explicitly_set(name)


def get_field(name: str) -> Any:
    return _state.get_field(name)


def set_field(name: str, value: object) -> None:
    _state.set_field(name, value)


def prepare() -> None:
    _state.prepare()


def upgrade(reason: str) -> object:
    return _state.upgrade(reason)


def maybe_upgrade(reason: str) -> bool:
    return _state._maybe_upgrade(reason)


def ensure_eager() -> None:
    _state.ensure_eager()


def boot(namespace: dict[str, Any], apply_timezone: Callable[[], None]) -> None:
    """Bind the facade (its namespace and timezone applier), then prepare or go eager."""
    _state.bind_facade(namespace, apply_timezone)
    _state.boot()


def is_full() -> bool:
    return _state.is_full()


def boot_state() -> dict[str, Any]:
    return _state.boot_state()


def process_launch_facts() -> LaunchFacts:
    return _state.launch_facts()


def settings_view() -> _SettingsView:
    return _state.view


def refresh_data_plane_settings() -> None:
    _state.refresh_data_plane_settings()


def set_wait_timeout(seconds: float) -> None:
    _state.wait_timeout_seconds = seconds


# meiva:note
# Enter the legacy owner's in-flight build window on the current thread.
def in_flight_build() -> AbstractContextManager[None]:
    return _state.in_flight_build()
