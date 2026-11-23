# meiva:link gateway/alerts/docs/alerts.md

from __future__ import annotations

import os
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from base.host.boundary.untyped import object_of

_SITE_CONTACT = Path("lgtm/alerting/contact.yml")
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_MAX_CONTACT_BYTES = 256 * 1024

type NonBlank = Annotated[str, StringConstraints(pattern=r"\S")]
type OrgId = Annotated[int, Field(ge=1)]
type Label = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")]
type Interval = Annotated[
    str,
    StringConstraints(
        min_length=1,
        pattern=r"^(?:0|(?:[0-9]+y)?(?:[0-9]+w)?(?:[0-9]+d)?(?:[0-9]+h)?(?:[0-9]+m)?(?:[0-9]+s)?(?:[0-9]+ms)?)$",
    ),
]
type ObjectMatcher = Annotated[
    tuple[Label, Literal["=", "!=", "=~", "!~"], str], Field(strict=False)
]


class _ProvisioningModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


class _WebhookSettings(_ProvisioningModel):
    url: NonBlank
    http_method: Literal["POST", "PUT"] = Field(default="POST", alias="httpMethod")
    authorization_scheme: str = ""
    authorization_credentials: str = ""


class _ContactIdentity(_ProvisioningModel):
    uid: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,40}$")]


class _Receiver(_ContactIdentity):
    type: Literal["webhook"]
    disable_resolve_message: bool = Field(default=False, alias="disableResolveMessage")
    settings: _WebhookSettings


class _ContactPoint(_ProvisioningModel):
    org_id: OrgId = Field(default=1, alias="orgId")
    name: NonBlank
    receivers: Annotated[list[_Receiver], Field(min_length=1)]


class _DeleteContact(_ContactIdentity):
    org_id: OrgId = Field(default=1, alias="orgId")


class _Route(_ProvisioningModel):
    receiver: NonBlank | None = None
    group_by: list[Label | Literal["..."]] = Field(default_factory=list)
    matchers: list[str] = Field(default_factory=list, max_length=0)
    object_matchers: list[ObjectMatcher] = Field(default_factory=list)
    group_wait: Interval | None = None
    group_interval: Interval | None = None
    repeat_interval: Interval | None = None
    continue_: bool = Field(default=False, alias="continue")
    routes: list[_Route] = Field(default_factory=list)

    @field_validator("group_interval", "repeat_interval")
    @classmethod
    def positive_interval(cls, value: str | None) -> str | None:
        if value is not None and not any(digit in value for digit in "123456789"):
            raise ValueError("interval must be positive")
        return value

    @field_validator("group_by")
    @classmethod
    def distinct_group_labels(cls, labels: list[str]) -> list[str]:
        if len(set(labels)) != len(labels) or ("..." in labels and len(labels) > 1):
            raise ValueError("grouping needs distinct labels or the single wildcard")
        return labels

    def receivers(self) -> set[str]:
        names: set[str] = {self.receiver} if self.receiver is not None else set()
        return names.union(*(route.receivers() for route in self.routes))


class _Policy(_Route):
    org_id: OrgId = Field(default=1, alias="orgId")

    @model_validator(mode="after")
    def check_root(self) -> Self:
        if self.receiver is None:
            raise ValueError("the root policy must name a receiver")
        if self.object_matchers or self.continue_:
            raise ValueError("the root policy must match all alerts")
        return self


class _AlertContact(_ProvisioningModel):
    api_version: int = Field(ge=1, le=1, alias="apiVersion")
    contact_points: list[_ContactPoint] = Field(min_length=1, alias="contactPoints")
    policies: Annotated[list[_Policy], Field(min_length=1)]
    delete_contact_points: list[_DeleteContact] = Field(
        default_factory=list, alias="deleteContactPoints"
    )

    @model_validator(mode="after")
    def check_references(self) -> Self:
        contacts = {(contact.org_id, contact.name.strip()) for contact in self.contact_points}
        orgs = {policy.org_id for policy in self.policies}
        if len(orgs) != len(self.policies):
            raise ValueError("only one root policy per organization is allowed")
        for policy in self.policies:
            if any((policy.org_id, name) not in contacts for name in policy.receivers()):
                raise ValueError(
                    "every policy receiver must name a contact point in its organization"
                )
        uids = {
            (contact.org_id, receiver.uid)
            for contact in self.contact_points
            for receiver in contact.receivers
        }
        if len(uids) != sum(len(contact.receivers) for contact in self.contact_points):
            raise ValueError("receiver UIDs must be unique within each organization")
        if any((deletion.org_id, deletion.uid) in uids for deletion in self.delete_contact_points):
            raise ValueError("a receiver cannot be defined and deleted in the same file")
        return self


@dataclass(frozen=True)
class AlertContactOverride:
    content: str | None


def _read_override(meiva_home: Path) -> bytes:
    with ExitStack() as opened:
        directory = os.open(meiva_home, _DIRECTORY)
        _ = opened.callback(os.close, directory)
        for segment in _SITE_CONTACT.parts[:-1]:
            directory = os.open(segment, _DIRECTORY, dir_fd=directory)
            _ = opened.callback(os.close, directory)
        fd = os.open(_SITE_CONTACT.name, _READ_FILE, dir_fd=directory)
        _ = opened.callback(os.close, fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("must be a regular file")
        if info.st_size > _MAX_CONTACT_BYTES:
            raise ValueError(f"exceeds {_MAX_CONTACT_BYTES} bytes")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            content = stream.read(_MAX_CONTACT_BYTES + 1)
        if len(content) > _MAX_CONTACT_BYTES:
            raise ValueError(f"exceeds {_MAX_CONTACT_BYTES} bytes")
    return content


def read_alert_contact_override(meiva_home: Path) -> AlertContactOverride:
    path = meiva_home / _SITE_CONTACT
    try:
        raw = _read_override(meiva_home)
    except FileNotFoundError:
        return AlertContactOverride(None)
    except OSError as exc:
        raise ValueError(
            f"Grafana alerting override {path} cannot be opened without following links: {exc.strerror}"
        ) from None
    except ValueError as exc:
        raise ValueError(f"Grafana alerting override {path} {exc}") from None

    try:
        content = raw.decode("utf-8")
        document = object_of(yaml.safe_load, content)
    except (yaml.YAMLError, UnicodeError):
        raise ValueError(f"Grafana alerting override {path} must be valid UTF-8 YAML") from None
    try:
        _ = _AlertContact.model_validate(document)
    except ValidationError as exc:
        error = exc.errors(include_input=False, include_context=False, include_url=False)[0]
        loc = error["loc"][:-1] if error["type"] == "extra_forbidden" else error["loc"]
        field = ".".join(str(part) for part in loc) or "document"
        raise ValueError(
            f"Grafana alerting override {path} is invalid at {field}: {error['msg']}"
        ) from None
    return AlertContactOverride(content)
