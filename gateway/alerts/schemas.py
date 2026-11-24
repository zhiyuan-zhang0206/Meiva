"""The Alertmanager standard webhook payload, as Grafana's embedded Alertmanager delivers it to a
webhook contact point: one POST carries ``status`` + ``alerts[]``, each alert an instance of a rule
(labels/annotations/startsAt/endsAt/generatorURL)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# The webhook vocabulary Alertmanager sends.
WebhookStatus = Literal["firing", "resolved"]


class AlertWebhookAlert(BaseModel):
    """One alert instance inside an Alertmanager webhook POST.

    ``labels`` / ``annotations`` are free-form; extra per-alert fields
    Alertmanager adds over time (``silenceURL``, ``imageURL``, …) are
    tolerated, not rejected.
    """

    model_config = ConfigDict(extra="ignore")

    status: WebhookStatus = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: str = Field(default="", alias="startsAt")
    ends_at: str = Field(default="", alias="endsAt")
    generator_url: str = Field(default="", alias="generatorURL")


class AlertWebhookPayload(BaseModel):
    """Top level of an Alertmanager webhook POST.

    Tolerates both the full Alertmanager v4 envelope (version/groupKey/
    receiver/commonLabels/… — ignored here) and the slimmer Grafana-managed
    webhook shape; only ``status`` + ``alerts[]`` matter to the gateway.
    """

    model_config = ConfigDict(extra="ignore")

    status: WebhookStatus | None = None
    alerts: list[AlertWebhookAlert] = Field(default_factory=list[AlertWebhookAlert])
