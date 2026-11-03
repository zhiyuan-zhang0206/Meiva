# meiva:note
# Resolved slices remain fixed for one turn; lists become tuples. Unpinned unsliced reads use the explicit owner live defaults, while pins win without leaking to another agent. ModelOverrides.from_pins reads only supplied pins, so the gateway need not own unrelated config domains. Plugin instances are cached within their own configuration view, not globally.
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import Field, dataclass, field, fields
from types import MappingProxyType
from typing import Literal, overload

from pydantic import BaseModel

from base.host.boundary.json_values import is_object_list
from base.host.env.agent_env import ModelOverrides as ModelOverrides
from base.host.env.agent_env import built_from_values
from base.host.env.config_lite_table import FIELD_DOMAINS

Cadence = Literal["once_per_compaction", "every_time"]


@dataclass(frozen=True)
class AgentBrain:
    llm_model: str


@dataclass(frozen=True)
class Prompt:
    system_prompt_extra: tuple[str, ...]
    skills_to_inject_into_system_prompt: tuple[str, ...]
    skills_to_expand_at_start: tuple[str, ...]
    agent_communication_style: str | None
    prompt_codeact_enabled: bool
    prompt_sequential_tool_calls_enabled: bool
    sdk_disable: tuple[str, ...]
    to_user_soft_limit_words: int
    to_user_limit_reminder_cadence: Cadence


@dataclass(frozen=True)
class MemoryRecall:
    passive_memory_recall_enabled: bool
    memory_recall_inject_threshold: float
    memory_recall_inject_max: int
    memory_recall_deadline_seconds: float
    memory_write_check_deadline_seconds: float
    memory_write_dedup_threshold: float
    memory_inherit_depth: int


@dataclass(frozen=True)
class SkillRecall:
    skill_recall_enabled: bool
    skill_recall_model: str
    skill_recall_threshold: float
    skill_recall_peer_threshold: float
    skill_recall_max: int
    skill_recall_deadline_seconds: float
    skill_recall_peer_messages: bool
    skill_recall_peer_gate_work_floor: float
    skill_recall_peer_gate_method_ceiling: float
    skill_recall_peer_gate_max_chars: int
    skill_recall_peer_gate_deadline_seconds: float


@dataclass(frozen=True)
class HistoryDump:
    history_dump_enabled: bool
    history_dump_keep: int


@dataclass(frozen=True)
class SdkReminders:
    sdk_code_reminder_cadence: Cadence
    sdk_nameerror_hint_enabled: bool
    agent_reply_reminder_cadence: Cadence


@dataclass(frozen=True)
class LlmCallPolicy:
    llm_fatal_provider_error_types: str


@dataclass(frozen=True)
class Sandbox:
    eval_isolation: bool
    eval_network_allowlist: tuple[str, ...]
    exec_timeout_seconds: float
    syntax_fix_ruff_format: bool


@dataclass(frozen=True)
class AgentKernel:
    checkpoint_interval: int
    heartbeat_pause_max_seconds: float


# meiva:note
# The knobs a hosted turn hands its external CLI. Separate from `kernel` because they
# decide how another program is launched, not how this agent's own loop runs, and they
# are the daemon-domain settings a turn resolves.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
@dataclass(frozen=True)
class HarnessCli:
    harness_skip_permissions: bool


def _value(
    pins: Mapping[str, object], name: str, default_reader: Callable[[str, str], object]
) -> object:

    value = pins[name] if name in pins else default_reader(FIELD_DOMAINS[name], name)
    return tuple(value) if is_object_list(value) else value


# meiva:note
# One setting of this agent: its pin if it has one, else the live default. The answer is `object`
# because a pin or a reader can hold any type; a caller narrows it to what it reads (lists come back
# as tuples).
def agent_setting(
    name: str,
    pins: Mapping[str, object] | None = None,
    *,
    default_reader: Callable[[str, str], object],
) -> object:

    return _value(pins or {}, name, default_reader)


def _slice[S](
    slice_type: type[S],
    slice_fields: Iterable[Field[object]],
    pins: Mapping[str, object],
    default_reader: Callable[[str, str], object],
) -> S:
    return built_from_values(
        slice_type, {f.name: _value(pins, f.name, default_reader) for f in slice_fields}
    )


class PluginConfigView:
    __slots__ = ("_cache", "_configs", "_overrides")

    def __init__(
        self, configs: Mapping[str, BaseModel], overrides: Mapping[str, Mapping[str, object]]
    ) -> None:
        self._configs = configs
        self._overrides = {p: dict(fields) for p, fields in overrides.items()}
        self._cache: dict[str, BaseModel] = {}

    def flat(self) -> dict[str, object]:

        return {key: value for fields in self._overrides.values() for key, value in fields.items()}

    def config_for(self, plugin: str) -> BaseModel:

        base = self._configs[plugin]
        updates = self._overrides.get(plugin)
        if not updates:
            return base
        cached = self._cache.get(plugin)
        if cached is not None and type(cached) is type(base):
            return cached
        built = type(base)(**{**base.model_dump(), **updates})
        self._cache[plugin] = built
        return built

    def configs(self) -> dict[str, BaseModel]:

        return {plugin: self.config_for(plugin) for plugin in self._configs}


@dataclass(frozen=True)
class AgentSlices:
    brain: AgentBrain
    prompt: Prompt
    memory: MemoryRecall
    skill_recall: SkillRecall
    history_dump: HistoryDump
    sdk_reminders: SdkReminders
    llm_policy: LlmCallPolicy
    overrides: ModelOverrides
    sandbox: Sandbox
    kernel: AgentKernel
    harness_cli: HarnessCli

    pins: Mapping[str, object]
    plugin_pins: Mapping[str, Mapping[str, object]]
    _plugin_view: PluginConfigView = field(repr=False, compare=False)
    _default_reader: Callable[[str, str], object] = field(repr=False, compare=False)

    @classmethod
    def resolve(
        cls,
        pins: Mapping[str, object] | None = None,
        plugin_pins: Mapping[str, Mapping[str, object]] | None = None,
        *,
        default_reader: Callable[[str, str], object],
        plugin_configs: Mapping[str, BaseModel] | None = None,
        brain: AgentBrain | None = None,
    ) -> AgentSlices:

        pins = pins or {}
        plugin_pins = plugin_pins or {}
        return cls(
            brain=brain
            if brain is not None
            else _slice(AgentBrain, fields(AgentBrain), pins, default_reader),
            prompt=_slice(Prompt, fields(Prompt), pins, default_reader),
            memory=_slice(MemoryRecall, fields(MemoryRecall), pins, default_reader),
            skill_recall=_slice(SkillRecall, fields(SkillRecall), pins, default_reader),
            history_dump=_slice(HistoryDump, fields(HistoryDump), pins, default_reader),
            sdk_reminders=_slice(SdkReminders, fields(SdkReminders), pins, default_reader),
            llm_policy=_slice(LlmCallPolicy, fields(LlmCallPolicy), pins, default_reader),
            overrides=_slice(ModelOverrides, fields(ModelOverrides), pins, default_reader),
            sandbox=_slice(Sandbox, fields(Sandbox), pins, default_reader),
            kernel=_slice(AgentKernel, fields(AgentKernel), pins, default_reader),
            harness_cli=_slice(HarnessCli, fields(HarnessCli), pins, default_reader),
            pins=MappingProxyType(dict(pins)),
            plugin_pins=MappingProxyType({p: dict(f) for p, f in plugin_pins.items()}),
            _plugin_view=PluginConfigView(plugin_configs or {}, plugin_pins),
            _default_reader=default_reader,
        )

    @overload
    def read(
        self,
        domain: str,
        field: Literal[
            "understanding_chunk_ratio",
            "node_stall_dump_seconds",
            "exec_node_timeout_seconds",
            "exec_timeout_seconds",
            "llm_compact_timeout_seconds",
            "llm_retry_max_total_seconds",
            "llm_retry_initial_interval_seconds",
            "llm_retry_max_interval_seconds",
        ],
    ) -> float: ...

    @overload
    def read(
        self,
        domain: str,
        field: Literal[
            "exec_output_max_chars",
            "llm_retry_max_attempts",
            "max_chain_depth",
            "max_document_bytes",
            "max_references",
            "max_resource_bytes",
            "memory_inherit_max_block_chars",
            "memory_inherit_max_total_chars",
            "memory_per_agent_index_max_lines",
            "timeline_default_limit",
            "to_user_soft_limit_words",
        ],
    ) -> int: ...

    @overload
    def read(
        self,
        domain: str,
        field: Literal[
            "understanding_enabled",
            "security_scan_enabled",
            "message_timestamps",
            "workspace_in_system_prompt",
            "prompt_sdk_overview_enabled",
            "prompt_knowledge_cutoff_enabled",
            "memory_index_inject_enabled",
            "memory_per_agent_inject_enabled",
        ],
    ) -> bool: ...

    @overload
    def read(
        self,
        domain: str,
        field: Literal["prompt_memory_behavior_enabled"],
    ) -> bool | None: ...

    @overload
    def read(
        self,
        domain: str,
        field: Literal["sdk_expand_in_system_prompt"],
    ) -> list[str]: ...

    @overload
    def read(
        self,
        domain: str,
        field: Literal["llm_override", "llm_model"],
    ) -> str: ...

    @overload
    def read(self, domain: Literal["sandbox"], field: str) -> int: ...

    @overload
    def read(self, domain: str, field: str) -> object: ...

    def read(self, domain: str, field: str) -> object:

        if field in self.pins:
            return self.pins[field]
        return self._default_reader(domain, field)

    def plugin_config(self, plugin: str) -> BaseModel:

        return self._plugin_view.config_for(plugin)

    def plugin_configs(self) -> dict[str, BaseModel]:

        return self._plugin_view.configs()

    def overlay(self) -> dict[str, object]:

        return {**self.pins, **self._plugin_view.flat()}
