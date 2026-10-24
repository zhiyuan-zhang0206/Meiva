# meiva:note
# The committed task publication facts, separate from the mutable SDK Task and from Event
# attribute mappings. Event lines reuse the canonical JSONB representation already admitted
# by the transaction. This module stays value-only on the fleet plugin's cold import path.
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskPublication:
    actor: int
    task_id: int
    event_lines: tuple[str, ...]
    closed_title: str | None
