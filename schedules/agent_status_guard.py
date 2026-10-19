from __future__ import annotations


# meiva:note
# A stored schedule script can outlive the installed SDK's `AgentStatus`, so the guard
# exits before any work starts and names the schedule and every missing member: the
# failure is one schedule-record error instead of a mid-run `AttributeError`.
def ensure_agent_status_members(
    agent_status: type[object],
    required_members: set[str],
    *,
    schedule_name: str,
) -> None:
    missing: list[str] = []
    for member_name in sorted(required_members):
        try:
            getattr(agent_status, member_name)
        except AttributeError:
            missing.append(member_name)
    if missing:
        raise SystemExit(
            f"schedule {schedule_name!r} is missing AgentStatus members: {', '.join(missing)}"
        )
