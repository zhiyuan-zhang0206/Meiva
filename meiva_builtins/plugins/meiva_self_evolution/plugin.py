"""meiva_self_evolution plugin — one conditional reminder to keep a lesson.

The plugin contributes a compact-reminder section and seeds the workspace file
that section names. It adds no SDK surface: the reminder is the whole
contribution, and the agent decides in each window whether anything qualifies.
"""

from __future__ import annotations

__description__ = (
    "Remind the agent at the compaction checkpoint to record a lesson in "
    "to_do_better.md when one is worth keeping"
)
