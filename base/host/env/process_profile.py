# meiva:note
# The process profiles a launcher can select, and the one door from a delivered string to them.
#
# `MEIVA_PROCESS_PROFILE` is set by the process launcher, never by hand. Every branch that
# decides on it (which config domains a process builds, which credential it presents, which
# projections the env authority keeps) takes a `ProcessProfile`, so a misspelt marker is refused
# where the string enters (`parse_process_profile`) instead of passing for "some other profile"
# in a comparison. The config layer maps each profile to its domains
# (`base.config.profiles.PROCESS_PROFILES`).
from __future__ import annotations

from typing import Literal

MEIVA_PROCESS_PROFILE_ENV = "MEIVA_PROCESS_PROFILE"
ProcessProfile = Literal["gateway", "agent", "runner"]
PROCESS_PROFILE_NAMES: tuple[ProcessProfile, ...] = ("gateway", "agent", "runner")


def parse_process_profile(value: str) -> ProcessProfile:
    for profile in PROCESS_PROFILE_NAMES:
        if profile == value:
            return profile
    raise profile_unknown_error(value)


def profile_unknown_error(profile: str) -> ValueError:
    return ValueError(
        f"{MEIVA_PROCESS_PROFILE_ENV}={profile!r} is not a known process profile; "
        f"must be one of {sorted(PROCESS_PROFILE_NAMES)} — the marker is set by the "
        f"process launcher, not by hand"
    )
