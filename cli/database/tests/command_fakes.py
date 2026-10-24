# meiva:note
# The expected session names the command tests assert on.


from __future__ import annotations

__all__ = ["sess"]


def sess(service: str) -> str:
    # meiva:note
    # Expected composed session name (`meiva-<service>` — no cluster segment;
    # the per-home session backend scopes sessions).
    return f"meiva-{service}"
