"""Frontend telemetry ingestion — request schema for POST /api/frontend-telemetry.

The browser telemetry module (`ui/web/src/lib/telemetry/telemetry.ts`) batches tracked
interactions and posts them here. Validation is strict on shape (fail fast:
a malformed batch is a bug in our own client) and lenient on content (the
semantic allowlist — which elements exist — lives in the frontend module, the
single producer). No attribute may carry free text: `value` is a sanitized
scalar rendering (bool / number / ≤64-char string) produced by the client,
never raw input content.
"""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

Height = Annotated[float, Field(strict=True, ge=0, le=100_000, allow_inf_nan=False)]
Bottom = Annotated[float, Field(strict=True, ge=-100_000, le=100_000, allow_inf_nan=False)]


class ViewportGeometryIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    incident: Annotated[int, Field(strict=True, ge=1, le=3)]
    phase: Literal["anomaly", "recovered", "timeout"]
    reason: Literal["mount", "viewport-change", "box-change", "visible", "deadline"]
    elapsed_ms: Annotated[float, Field(strict=True, ge=0, le=86_400_000, allow_inf_nan=False)]
    layout_height: Height
    visual_height: Height | None
    visual_scale: Annotated[float, Field(strict=True, ge=0.01, le=100, allow_inf_nan=False)] | None
    html_inline_height: Height | None
    html_height: Height
    body_height: Height
    shell_height: Height
    shell_bottom: Bottom
    composer_height: Height | None
    composer_bottom: Bottom | None


# One tracked interaction as the client saw it. `ts` is the client-side
# wall-clock (ms epoch) and is advisory only — the server stamps rows with
# its own clock so a buggy or offline client cannot inject timestamps.
# `key` / `value` carry bounded setting-change or SSE transport metadata;
# other events omit them (None).
class FrontendInteractionIn(BaseModel):
    """One tracked interaction from the browser."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: str = Field(pattern=r"^[a-z0-9/_-]{1,64}$")
    element: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    key: str | None = Field(default=None, pattern=r"^[a-z0-9._-]{1,128}$")
    value: str | None = Field(default=None, max_length=64)
    ts: int | None = Field(default=None, ge=0)
    geometry: ViewportGeometryIn | None = None

    @model_validator(mode="after")
    def geometry_event(self) -> "FrontendInteractionIn":
        if self.element == "viewport-anomaly":
            if (
                self.geometry is None
                or self.page != "home"
                or self.key is not None
                or self.value is not None
            ):
                raise ValueError(
                    "viewport-anomaly requires geometry, page=home and no scalar metadata"
                )
        elif self.geometry is not None:
            raise ValueError("geometry is restricted to viewport-anomaly")
        return self


class FrontendTelemetryBatch(BaseModel):
    """POST /api/frontend-telemetry body — one tab's buffered interactions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(pattern=r"^[0-9a-f-]{8,64}$")
    events: list[FrontendInteractionIn] = Field(min_length=1, max_length=200)
