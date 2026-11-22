from __future__ import annotations

from collections import OrderedDict
from collections.abc import Container, Sequence
from datetime import datetime, timedelta
from threading import Lock
from typing import Protocol

from base.agents.model.harness_models import resolve_harness_catalog_model
from base.api_contracts.agent_contract import Harness
from base.lm.pricing.archive import load_archive
from base.log import logger

_UNPRICED_PROBE_COOLDOWN = timedelta(hours=24)
_UNPRICED_PROBE_MAX_ENTRIES = 512
_CLAUDE_CLI_SELECTOR_ALIASES = frozenset({"default", "sonnet", "opus", "haiku"})


class _NamedModel(Protocol):
    @property
    def id(self) -> str: ...


class ProbedHarnessRow(Protocol):
    @property
    def harness(self) -> Harness: ...

    @property
    def models(self) -> Sequence[_NamedModel]: ...


class UnpricedProbeReporter:
    def __init__(
        self,
        *,
        cooldown: timedelta = _UNPRICED_PROBE_COOLDOWN,
        max_entries: int = _UNPRICED_PROBE_MAX_ENTRIES,
    ) -> None:
        self._cooldown = cooldown
        self._max_entries = max_entries
        self._seen: OrderedDict[tuple[Harness, str], datetime | None] = OrderedDict()
        self._lock = Lock()

    def should_emit(
        self, harness: Harness, canonical_model: str, *, now: datetime | None = None
    ) -> bool:
        key = (harness, canonical_model)
        with self._lock:
            if key in self._seen:
                previous = self._seen[key]
                if now is None or previous is None or now - previous < self._cooldown:
                    return False
            self._seen[key] = now
            self._seen.move_to_end(key)
            while len(self._seen) > self._max_entries:
                _ = self._seen.popitem(last=False)
            return True


def report_unpriced_probed_models(
    probes: Sequence[ProbedHarnessRow],
    *,
    known_priced_models: Container[str] | None = None,
    now: datetime | None = None,
    reporter: UnpricedProbeReporter | None = None,
) -> tuple[tuple[Harness, str], ...]:
    priced = frozenset(load_archive()) if known_priced_models is None else known_priced_models
    dedup = UnpricedProbeReporter() if reporter is None else reporter
    emitted: list[tuple[Harness, str]] = []
    for probe in probes:
        if probe.harness is Harness.MEIVA or not probe.models:
            continue
        for model in probe.models:
            canonical = resolve_harness_catalog_model(model.id, priced)
            if (
                probe.harness is Harness.CLAUDE_CODE
                and canonical.lower() in _CLAUDE_CLI_SELECTOR_ALIASES
            ):
                continue
            if canonical in priced:
                continue
            if dedup.should_emit(probe.harness, canonical, now=now):
                logger.warning(
                    "[harness] probed unpriced {harness} model {canonical_model!r} "
                    "(raw={raw_model!r}); add it to base/lm/pricing/pricing_catalog_archive.json",
                    event="harness_model_unpriced_probed",
                    harness=probe.harness.value,
                    raw_model=model.id,
                    canonical_model=canonical,
                )
                emitted.append((probe.harness, canonical))
    return tuple(emitted)
