from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Literal, Never, Protocol

from pydantic import BaseModel

from base.api_contracts.agent_contract import Harness
from base.api_contracts.metric_spec import MetricSpec
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_contract import DecisionProviderContribution, ProviderContribution
from base.lm.harness.handoff import HarnessContribution
from base.packages.plugins.contributions import Contribution
from base.packages.plugins.surface.inspector import InspectWidgetSpec

NoteBuilder = Callable[..., object | None]


class SectionFn(Protocol):
    @property
    def __name__(self) -> str: ...

    def __call__(self, slices: AgentSlices, /, *, catalog: ModelCatalog) -> str: ...


HookPoint = Literal["after_init", "before_llm", "before_exec", "after_exec"]
HOOK_POINTS: tuple[HookPoint, ...] = ("after_init", "before_llm", "before_exec", "after_exec")


# meiva:note
# A graph hook as the plugin ABI declares it: a named async callable the kernel calls with its state,
# runtime and config. The kernel's own types live above this package, so the parameters are `Never`
# here: a hook written against the kernel's types (`AgentState`, `Runtime`, `RunnableConfig`) is
# assignable to it, and the kernel, which knows those types, narrows a hook back to its own view
# (`agent.hooks`) before it calls it. The answer is the state update (or None).
class GraphHook(Protocol):
    @property
    def name(self) -> str: ...

    def __call__(
        self, state: Never, runtime: Never, config: Never, /
    ) -> Awaitable[Mapping[str, object] | None]: ...


PLUGIN_WRITABLE_BASE_FIELDS: frozenset[str] = frozenset({"messages"})

SDK_WRITTEN_BASE_FIELDS: frozenset[str] = frozenset(
    {"security_findings", "attach", "skill_reads", "ended_with", "ended_with_reason"}
)

DEFAULT_NOTE_RANK = 100


@dataclass(frozen=True)
class ContextNote:
    build: NoteBuilder
    on_fork: bool = False
    rank: int = DEFAULT_NOTE_RANK


@dataclass(frozen=True)
class SdkNamespace:
    name: str
    module: ModuleType | SimpleNamespace
    expand: bool = False


@dataclass(frozen=True)
class SdkMember:
    namespace: str
    name: str
    fn: Callable[..., object]


@dataclass(frozen=True)
class SdkWrap:
    target: str
    wrapper: Callable[..., object]


# meiva:link base/packages/plugins/docs/runtime-contributions.meiva.okf.md
@dataclass(frozen=True)
class PluginContributions:
    system_prompt_sections: tuple[SectionFn, ...] = ()
    compact_reminder_sections: tuple[SectionFn, ...] = ()
    context_notes: tuple[ContextNote, ...] = ()
    after_init: tuple[GraphHook, ...] = ()
    before_llm: tuple[GraphHook, ...] = ()
    before_exec: tuple[GraphHook, ...] = ()
    after_exec: tuple[GraphHook, ...] = ()
    state: tuple[type[BaseModel], ...] = ()
    metrics: tuple[MetricSpec, ...] = ()
    inspect_widgets: tuple[InspectWidgetSpec, ...] = ()
    providers: tuple[ProviderContribution, ...] = ()
    decision_providers: tuple[DecisionProviderContribution, ...] = ()
    sdk_namespaces: tuple[SdkNamespace, ...] = ()
    sdk_members: tuple[SdkMember, ...] = ()
    sdk_wraps: tuple[SdkWrap, ...] = ()
    skill_sources: tuple[Callable[[], list[Path]], ...] = ()
    sdk_expansions: tuple[str, ...] = ()
    harness_contributions: tuple[HarnessContribution, ...] = ()
    config: type[BaseModel] | None = None
    flags: tuple[str, ...] = ()

    def merged(self, other: PluginContributions) -> PluginContributions:
        if self.config is not None and other.config is not None:
            raise ValueError("config is declared by more than one face")
        return PluginContributions(
            system_prompt_sections=self.system_prompt_sections + other.system_prompt_sections,
            compact_reminder_sections=self.compact_reminder_sections
            + other.compact_reminder_sections,
            context_notes=self.context_notes + other.context_notes,
            after_init=self.after_init + other.after_init,
            before_llm=self.before_llm + other.before_llm,
            before_exec=self.before_exec + other.before_exec,
            after_exec=self.after_exec + other.after_exec,
            state=self.state + other.state,
            metrics=self.metrics + other.metrics,
            inspect_widgets=self.inspect_widgets + other.inspect_widgets,
            providers=self.providers + other.providers,
            decision_providers=self.decision_providers + other.decision_providers,
            sdk_namespaces=self.sdk_namespaces + other.sdk_namespaces,
            sdk_members=self.sdk_members + other.sdk_members,
            sdk_wraps=self.sdk_wraps + other.sdk_wraps,
            skill_sources=self.skill_sources + other.skill_sources,
            sdk_expansions=self.sdk_expansions + other.sdk_expansions,
            harness_contributions=self.harness_contributions + other.harness_contributions,
            config=self.config or other.config,
            flags=self.flags + other.flags,
        )

    def hooks(self, point: HookPoint) -> tuple[GraphHook, ...]:
        match point:
            case "after_init":
                return self.after_init
            case "before_llm":
                return self.before_llm
            case "before_exec":
                return self.before_exec
            case "after_exec":
                return self.after_exec

    def as_records(self, plugin: str) -> tuple[Contribution, ...]:
        return (
            *self._runtime_records(plugin),
            *self._data_records(plugin),
            *self._sdk_records(plugin),
        )

    def _runtime_records(self, plugin: str) -> tuple[Contribution, ...]:
        sections = (
            Contribution("systemPromptSections", fn.__name__, plugin, fn.__module__)
            for fn in self.system_prompt_sections
        )
        notes = (
            Contribution(
                "contextNotes", n.build.__name__, plugin, f"rank={n.rank} on_fork={n.on_fork}"
            )
            for n in self.context_notes
        )
        reminders = (
            Contribution("compactReminderSections", fn.__name__, plugin, fn.__module__)
            for fn in self.compact_reminder_sections
        )
        hooks = (
            Contribution("hooks", point, plugin, f"{type(h).__module__}.{type(h).__qualname__}")
            for point in HOOK_POINTS
            for h in self.hooks(point)
        )
        state = (
            Contribution(
                "state",
                name if name in PLUGIN_WRITABLE_BASE_FIELDS else f"{plugin}__{name}",
                plugin,
                f"{cls.__name__}.{name}",
            )
            for cls in self.state
            for name in cls.model_fields
        )
        return (*sections, *reminders, *notes, *hooks, *state)

    def _data_records(self, plugin: str) -> tuple[Contribution, ...]:
        metrics = (Contribution("metrics", spec.name, plugin, spec.title) for spec in self.metrics)
        widgets = (
            Contribution("inspectWidgets", spec.id, plugin, spec.kind)
            for spec in self.inspect_widgets
        )
        providers = (
            Contribution("providers", p.binding.prefix, plugin, p.binding.display_name)
            for p in self.providers
        )
        decision_providers = (
            Contribution("decisionProviders", p.prefix, plugin, p.display_name)
            for p in self.decision_providers
        )
        return (*metrics, *widgets, *providers, *decision_providers)

    def _sdk_records(self, plugin: str) -> tuple[Contribution, ...]:
        namespaces = (
            Contribution("sdkNamespaces", ns.name, plugin, getattr(ns.module, "__name__", ""))
            for ns in self.sdk_namespaces
        )
        members = (
            Contribution(
                "sdkMembers",
                f"{m.namespace}.{m.name}",
                plugin,
                f"{getattr(m.fn, '__module__', '?')}.{getattr(m.fn, '__qualname__', m.fn)}",
            )
            for m in self.sdk_members
        )
        promoted = (*(ns.name for ns in self.sdk_namespaces if ns.expand), *self.sdk_expansions)
        expansions = (
            Contribution("sdkExpansions", path, plugin, "expanded ahead of the framework list")
            for path in promoted
        )
        wraps = (
            Contribution(
                "sdkWraps",
                w.target,
                plugin,
                f"{getattr(w.wrapper, '__module__', '?')}.{getattr(w.wrapper, '__qualname__', w.wrapper)}",
            )
            for w in self.sdk_wraps
        )
        sources = (
            Contribution(
                "skillSources",
                getattr(p, "__name__", repr(p)),
                plugin,
                getattr(p, "__module__", "?"),
            )
            for p in self.skill_sources
        )
        config: tuple[Contribution, ...] = ()
        if self.config is not None:
            fields_text = ", ".join(self.config.model_fields) or "<none>"
            config = (
                Contribution("config", self.config.__name__, plugin, f"fields: {fields_text}"),
            )
        return (*namespaces, *members, *expansions, *wraps, *sources, *config)


# meiva:link base/packages/plugins/docs/runtime-contributions.meiva.okf.md
@dataclass(frozen=True)
class ExtensionRegistry:
    plugins: tuple[tuple[str, PluginContributions], ...] = ()

    def system_prompt_sections(self) -> Iterator[tuple[str, SectionFn]]:
        for plugin, contributions in self.plugins:
            for fn in contributions.system_prompt_sections:
                yield plugin, fn

    def context_notes(self) -> Iterator[tuple[str, ContextNote]]:
        for plugin, contributions in self.plugins:
            for note in contributions.context_notes:
                yield plugin, note

    def compact_reminder_sections(self) -> Iterator[tuple[str, SectionFn]]:
        for plugin, contributions in self.plugins:
            for fn in contributions.compact_reminder_sections:
                yield plugin, fn

    def hooks(self, point: HookPoint) -> Iterator[tuple[str, GraphHook]]:
        for plugin, contributions in self.plugins:
            for hook in contributions.hooks(point):
                yield plugin, hook

    def state_classes(self) -> Iterator[tuple[str, type[BaseModel]]]:
        for plugin, contributions in self.plugins:
            for cls in contributions.state:
                yield plugin, cls

    def sdk_wraps(self) -> Iterator[tuple[str, SdkWrap]]:
        for plugin, contributions in self.plugins:
            for wrap in contributions.sdk_wraps:
                yield plugin, wrap

    def metrics(self) -> Iterator[MetricSpec]:
        for _plugin, contributions in self.plugins:
            yield from contributions.metrics

    def inspect_widgets(self) -> Iterator[InspectWidgetSpec]:
        for _plugin, contributions in self.plugins:
            yield from contributions.inspect_widgets

    def harness_contributions(
        self, harness: Harness | None = None
    ) -> Iterator[tuple[str, HarnessContribution]]:
        for plugin, contributions in self.plugins:
            for hc in contributions.harness_contributions:
                if harness is None or harness in hc.harnesses:
                    yield plugin, hc

    def records(self, plugin: str) -> tuple[Contribution, ...]:
        return tuple(r for name, c in self.plugins if name == plugin for r in c.as_records(plugin))


EMPTY = ExtensionRegistry()
