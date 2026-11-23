# meiva:link gateway/alerts/docs/alerts.md

from __future__ import annotations

import os
import re
import stat
from collections.abc import Hashable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self, override

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from yaml.nodes import MappingNode, ScalarNode

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of

_SITE_CONTACT = Path("lgtm/alerting/contact.yml")
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_MAX_CONTACT_BYTES = 256 * 1024
_MAX_DURATION_NS = (1 << 63) - 1
_DURATION_UNITS_NS = {
    "y": 365 * 24 * 60 * 60 * 10**9,
    "w": 7 * 24 * 60 * 60 * 10**9,
    "d": 24 * 60 * 60 * 10**9,
    "h": 60 * 60 * 10**9,
    "m": 60 * 10**9,
    "s": 10**9,
    "ms": 10**6,
}
_DURATION = re.compile("".join(rf"(?:(?P<{unit}>[0-9]+){unit})?" for unit in _DURATION_UNITS_NS))
_MAX_REGEX_REPEAT = 1000
_REGEX_TOKENS = re.compile(
    r"\[(?P<character_class>\^?\]?(?:\\.|[^\\\]])*)\]|\\."
    r"|(?P<extension>\(\?(?!:))|(?P<opening>\((?:\?:)?)|(?P<closing>\))"
    r"|(?P<possessive>[*+?]\+)"
    r"|\{(?P<lower>[0-9]+)(?:,(?P<upper>[0-9]*))?\}(?P<range_possessive>\+)?",
    re.DOTALL,
)
_REGEX_ESCAPES = re.compile(r"\\(.)", re.DOTALL)
_COMMON_REGEX_ESCAPES = frozenset("aAbBdDfnrsStvwW")

_BRACED_ENV = re.compile(r"\$(?:__env)?\{([^}]+)\}")
_PLAIN_ENV = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*|[0-9*#$@!?{-])")
_WEBHOOK_ENV_FIELDS = {
    "url": "MEIVA_ALERTS_WEBHOOK_URL",
    "authorization_credentials": "MEIVA_ALERTS_WEBHOOK_TOKEN",
}


class _AlertYamlLoader(yaml.SafeLoader):
    @override
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, object]:
        entries = object_of(getattr, node, "value")
        if not is_object_list(entries):
            raise yaml.YAMLError("invalid mapping")
        keys: set[str] = set()
        for entry in entries:
            match entry:
                case (ScalarNode() as key, object()) if key.tag == "tag:yaml.org,2002:str":
                    name = self.construct_scalar(key)
                case _:
                    raise yaml.YAMLError("mapping keys must be strings")
            if name in keys:
                raise yaml.YAMLError("duplicate mapping key")
            keys.add(name)
        return super().construct_mapping(node, deep=deep)

    @override
    def construct_scalar(self, node: ScalarNode | MappingNode) -> str:
        value = super().construct_scalar(node)
        if node.tag == "tag:yaml.org,2002:bool" and value not in {
            "true",
            "True",
            "TRUE",
            "false",
            "False",
            "FALSE",
        }:
            raise yaml.YAMLError("boolean values must use true or false")
        if node.tag == "tag:yaml.org,2002:int" and not re.fullmatch(
            r"(?:0|[1-9][0-9]{0,18})", value
        ):
            raise yaml.YAMLError("integers must use unsigned decimal notation")
        return value


def _duration_nanoseconds(value: str) -> int:
    if value == "0":
        return 0
    match = _DURATION.fullmatch(value)
    if not value or match is None:
        raise ValueError("invalid Prometheus duration")
    total = 0
    for unit, nanoseconds in _DURATION_UNITS_NS.items():
        digits = (match.group(unit) or "").lstrip("0")
        if len(digits) > 19:
            raise ValueError("duration exceeds the signed 64-bit nanosecond range")
        total += int(digits or "0") * nanoseconds
    if total > _MAX_DURATION_NS:
        raise ValueError("duration exceeds the signed 64-bit nanosecond range")
    return total


def _is_common_escape(character: str) -> bool:
    return character in _COMMON_REGEX_ESCAPES or (
        character.isascii() and character.isprintable() and not character.isalnum()
    )


def _check_regex_repeat_sizes(tokens: list[re.Match[str]]) -> None:
    group_limits = [1]
    previous_group_end = -1
    previous_group_limit = 1
    for token in tokens:
        if token.group("opening"):
            group_limits.append(1)
        elif token.group("closing"):
            previous_group_limit = group_limits.pop()
            previous_group_end = token.end()
            group_limits[-1] = max(group_limits[-1], previous_group_limit)
        elif lower := token.group("lower"):
            repetitions = int(token.group("upper") or lower)
            atom_limit = previous_group_limit if token.start() == previous_group_end else 1
            expanded = repetitions * atom_limit
            if expanded > _MAX_REGEX_REPEAT:
                raise ValueError("matcher repetition exceeds Alertmanager's limit of 1000")
            group_limits[-1] = max(group_limits[-1], expanded)


def _check_regex(value: str) -> None:
    try:
        _ = re.compile(value)
    except (re.error, OverflowError, RecursionError):
        raise ValueError("matcher contains an invalid regular expression") from None
    tokens = list(_REGEX_TOKENS.finditer(value))
    for token in tokens:
        if token.group("extension") or token.group("possessive") or token.group("range_possessive"):
            raise ValueError("matcher uses an unsupported regular expression extension")
        escapes = [match[1] for match in _REGEX_ESCAPES.finditer(token[0])]
        if any(not _is_common_escape(ch) for ch in escapes) or (
            token.group("character_class") is not None and "b" in escapes
        ):
            raise ValueError("matcher uses an unsupported regular expression escape")
    _check_regex_repeat_sizes(tokens)


type NonBlank = Annotated[str, StringConstraints(pattern=r"\S")]
type OrgId = Annotated[int, Field(ge=1, le=(1 << 63) - 1)]
type Label = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")]
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
    group_wait: str | None = None
    group_interval: str | None = None
    repeat_interval: str | None = None
    continue_: bool = Field(default=False, alias="continue")
    routes: list[_Route] = Field(default_factory=list)

    @field_validator("group_wait", "group_interval", "repeat_interval")
    @classmethod
    def check_duration(cls, value: str | None, info: ValidationInfo) -> str | None:
        if (
            value is not None
            and _duration_nanoseconds(value) == 0
            and info.field_name != "group_wait"
        ):
            raise ValueError("interval must be positive")
        return value

    @field_validator("object_matchers")
    @classmethod
    def check_matchers(cls, matchers: list[ObjectMatcher]) -> list[ObjectMatcher]:
        for _, operator, value in matchers:
            if operator in {"=~", "!~"}:
                _check_regex(value)
        return matchers

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


def _is_nontext_setting(value: str) -> bool:
    if value in {"t", "T", "f", "F", "true", "True", "TRUE", "false", "False", "FALSE"}:
        return True
    if value != value.strip():
        return False
    try:
        _ = float(value)
    except ValueError:
        return bool(re.fullmatch(r"[+-]?0[xX][0-9a-fA-F_.]+[pP][+-]?[0-9_]+", value))
    return True


def _expand_scalar(value: str, *, field: str, env: Mapping[str, str]) -> str:
    def resolve(reference: re.Match[str]) -> str:
        name = reference[1]
        if name != _WEBHOOK_ENV_FIELDS.get(field) or name not in env:
            raise ValueError("unsupported environment reference in Grafana alerting override")
        return env[name]

    parts = value.split("$$")
    expanded = "$".join(_PLAIN_ENV.sub(resolve, _BRACED_ENV.sub(resolve, part)) for part in parts)
    if len(parts) == 1 and expanded != value and _is_nontext_setting(expanded):
        raise ValueError("expanded Grafana alerting override setting must remain text")
    return expanded


def _expanded_document(
    value: object, *, env: Mapping[str, str], seen: set[int], field: str = ""
) -> object:
    if isinstance(value, str):
        return _expand_scalar(value, env=env, field=field)
    if id(value) in seen:
        return value
    if is_object_dict(value):
        seen.add(id(value))
        for key, item in value.items():
            value[key] = _expanded_document(item, env=env, seen=seen, field=key)
    elif is_object_list(value):
        seen.add(id(value))
        for index, item in enumerate(value):
            value[index] = _expanded_document(item, env=env, seen=seen)
    return value


def read_alert_contact_override(
    meiva_home: Path, *, webhook_url: str, webhook_token: str
) -> AlertContactOverride:
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
        document = object_of(yaml.load, content, Loader=_AlertYamlLoader)
    except (UnicodeError, RecursionError):
        raise ValueError(f"Grafana alerting override {path} must be valid UTF-8 YAML") from None
    except yaml.YAMLError as exc:
        reason = str(exc) if type(exc) is yaml.YAMLError else "invalid YAML syntax"
        raise ValueError(f"Grafana alerting override {path}: {reason}") from None
    try:
        expanded = _expanded_document(
            document,
            seen=set(),
            env={
                "MEIVA_ALERTS_WEBHOOK_URL": webhook_url,
                "MEIVA_ALERTS_WEBHOOK_TOKEN": webhook_token,
            },
        )
    except ValueError as exc:
        raise ValueError(f"{exc}: {path}") from None
    except RecursionError:
        raise ValueError(
            f"Grafana alerting override {path} exceeds the YAML nesting limit"
        ) from None
    try:
        _ = _AlertContact.model_validate(expanded)
    except ValidationError as exc:
        error = exc.errors(include_input=False, include_context=False, include_url=False)[0]
        loc = error["loc"][:-1] if error["type"] == "extra_forbidden" else error["loc"]
        field = ".".join(str(part) for part in loc) or "document"
        raise ValueError(
            f"Grafana alerting override {path} is invalid at {field}: {error['msg']}"
        ) from None
    return AlertContactOverride(content)
