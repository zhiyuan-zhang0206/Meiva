# meiva:note
# Agent cross-machine transfer wire contracts: the public CLI-to-gateway REST request and
# response, the gateway-to-source-runner binary workspace export request and bundle manifest, and
# the four `POST /ops` envelopes that prepare, import, finalize and abort a target workspace.
# meiva:link gateway/agents/docs/agent-transfer.meiva.okf.md

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from base.agents.messages.envelope import validate_source
from base.api_contracts.caller_identity import CallerIdentity, is_caller_source


# meiva:note
# `POST /api/agents/{id}/transfer` request body.
#
# `to_machine` names the destination `agent-runner` machine. `source` defaults to `"user"` and is
# validated against `validate_source` so the wake prompt dispatched at cutover carries a valid
# claim-side provenance string. `message`, when given, must be non-blank after trimming.
class TransferAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_machine: str = Field(min_length=1, max_length=64)
    source: str = Field(default="user", min_length=1, max_length=64)
    message: str | None = Field(default=None, min_length=1, max_length=4096)

    @field_validator("to_machine")
    @classmethod
    def _check_to_machine(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("to_machine cannot be blank")
        return stripped

    @field_validator("source")
    @classmethod
    def _check_source(cls, value: str) -> str:
        if is_caller_source(value):
            CallerIdentity.from_source(value)
        validate_source(value)
        return value

    @field_validator("message")
    @classmethod
    def _check_message(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("message cannot be blank")
        return stripped


# meiva:note
# `POST /api/agents/{id}/transfer` response receipt after cutover commits.
class TransferAgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: int
    from_machine: str
    to_machine: str
    status: Literal["transferred"] = "transferred"
    agent_status: Literal["idling", "terminated"]
    files_transferred: int
    bytes_transferred: int
    killed_shell_sessions: list[int] = Field(default_factory=list[int])
    closed_pages: list[str] = Field(default_factory=list[str])


# meiva:note
# Tiny JSON request body (`< 1 KiB`, inside the 64 KiB `agent-ops` body limit) sent by the gateway
# to `POST /workspace/export` on the source runner. Carries agent_id and transfer_id so the source
# runner verifies the agent is still terminated and unmoved on this host before killing local shells
# and packing `<home>/workspaces/<agent_id>`.
class WorkspaceExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: int = Field(gt=0)
    transfer_id: str = Field(pattern=r"^[0-9a-f]{32}$")


# meiva:note
# One filesystem entry inside a transferred workspace bundle manifest.
#
# Regular files verify `size` and `sha256`; symlinks verify `link_target`; directories verify type
# and POSIX permission bits (`mode`). Special files (FIFOs, sockets, device nodes) are refused at
# export time.
class WorkspaceManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relpath: str = Field(min_length=1)
    kind: Literal["dir", "file", "symlink"]
    mode: int = Field(ge=0, le=0o7777)
    size: int = Field(ge=0)
    sha256: str | None = None
    link_target: str | None = None


# meiva:note
# Header manifest embedded inside a framed `MEIVA_WS_V1` workspace export stream.
class WorkspaceBundleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    agent_id: int = Field(gt=0)
    transfer_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_machine: str = Field(min_length=1)
    entries: list[WorkspaceManifestEntry]
    files_count: int = Field(ge=0)
    total_file_bytes: int = Field(ge=0)
    tar_bytes: int = Field(ge=0)
    tar_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    killed_shell_sessions: list[int] = Field(default_factory=list[int])


class WorkspaceTransferPreparePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: int = Field(gt=0)
    transfer_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class WorkspaceTransferPrepareResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    machine: str
    workspace_dir: str
    had_existing_workspace: bool


class WorkspaceTransferImportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: int = Field(gt=0)
    transfer_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    bundle_path: str = Field(min_length=1)
    bundle_bytes: int = Field(ge=0)
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkspaceTransferImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    machine: str
    workspace_dir: str
    files_transferred: int
    bytes_transferred: int


class WorkspaceTransferFinalizePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: int = Field(gt=0)
    transfer_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    role: Literal["source", "target"]


class WorkspaceTransferFinalizeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    machine: str
    cleaned: bool


class WorkspaceTransferAbortPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: int = Field(gt=0)
    transfer_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class WorkspaceTransferAbortResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    machine: str
    restored_previous: bool
