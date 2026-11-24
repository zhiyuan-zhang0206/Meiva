from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, NonNegativeInt

FiringState = Literal["ok", "unavailable", "not_configured"]


class FiringAlerts(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: FiringState
    firing: NonNegativeInt | None = None
    critical: NonNegativeInt | None = None
    error: NonNegativeInt | None = None
    warning: NonNegativeInt | None = None
