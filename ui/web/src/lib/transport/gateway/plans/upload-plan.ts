import type { DeliveredUploadAcceptance, StagedFile } from "../../../contracts/types";
import { keyedHeaders } from "./key-plan";

interface SubmittedFile {
  name: string;
  size: number;
  contentType: string;
}

export interface StagedUploadPlan {
  path: string;
  headers: Record<string, string>;
  submitted: SubmittedFile;
}

export interface UploadBatchPlan {
  path: string;
  headers: Record<string, string>;
  body: string;
  fileIds: readonly string[];
}

export function planStagedUpload(
  agentId: number,
  file: { name: string; size: number; type: string },
  operationKey: string,
): StagedUploadPlan {
  if (!Number.isSafeInteger(agentId) || agentId <= 0) throw new Error("invalid upload agent ID");
  const headers = keyedHeaders(operationKey);
  return {
    path: `/api/keyed/v1/agents/${agentId}/uploads/staged`,
    headers,
    submitted: {
      name: file.name,
      size: file.size,
      contentType: file.type || "application/octet-stream",
    },
  };
}

export function planUploadBatch(
  agentId: number,
  fileIds: readonly string[],
  text: string,
  operationKey: string,
): UploadBatchPlan {
  if (!Number.isSafeInteger(agentId) || agentId <= 0) throw new Error("invalid upload agent ID");
  const headers = keyedHeaders(operationKey);
  if (fileIds.length === 0) throw new Error("upload batch must contain files");
  if (!fileIds.every((id) => typeof id === "string" && /^[0-9a-f]{32}$/.test(id))) {
    throw new Error("invalid staged file ID");
  }
  return {
    path: `/api/keyed/v1/agents/${agentId}/uploads`,
    headers: { "content-type": "application/json", ...headers },
    body: JSON.stringify({ file_ids: [...fileIds], text }),
    fileIds: [...fileIds],
  };
}

function failureDetail(status: number, responseText: string): string {
  let detail = `HTTP ${status}`;
  try {
    const error: unknown = JSON.parse(responseText);
    if (error && typeof error === "object" && "detail" in error && typeof error.detail === "string" && error.detail) {
      detail = error.detail;
    }
  } catch {
    detail = `HTTP ${status}`;
  }
  return detail;
}

const UNCONFIRMED_STAGED = "Staged upload receipt is unconfirmed";
const UNCONFIRMED_ACCEPTANCE = "Upload source acceptance is unconfirmed";

function isConfirmedStagedFile(
  staged: unknown,
  plan: StagedUploadPlan,
): staged is StagedFile {
  return (
    typeof staged === "object" &&
    staged !== null &&
    "file_id" in staged &&
    typeof staged.file_id === "string" &&
    /^[0-9a-f]{32}$/.test(staged.file_id) &&
    "name" in staged &&
    typeof staged.name === "string" &&
    staged.name.length > 0 &&
    "size" in staged &&
    staged.size === plan.submitted.size &&
    "content_type" in staged &&
    staged.content_type === plan.submitted.contentType &&
    "sha256" in staged &&
    typeof staged.sha256 === "string" &&
    /^[0-9a-f]{64}$/.test(staged.sha256)
  );
}

export function confirmStagedUpload(
  status: number,
  responseText: string,
  plan: StagedUploadPlan,
): StagedFile {
  if (status !== 201) throw new Error(failureDetail(status, responseText));
  const staged: unknown = JSON.parse(responseText);
  if (!isConfirmedStagedFile(staged, plan)) {
    throw new Error(UNCONFIRMED_STAGED);
  }
  return staged;
}

function isConfirmedUploadAcceptance(
  accepted: unknown,
  agentId: number,
  plan: UploadBatchPlan,
): accepted is DeliveredUploadAcceptance {
  if (
    !accepted ||
    typeof accepted !== "object" ||
    !("batch_id" in accepted) ||
    typeof accepted.batch_id !== "string" ||
    !/^[0-9a-f]{32}$/.test(accepted.batch_id) ||
    !("agent_id" in accepted) ||
    accepted.agent_id !== agentId ||
    !("directory" in accepted) ||
    typeof accepted.directory !== "string" ||
    accepted.directory.length === 0 ||
    !("status_url" in accepted) ||
    accepted.status_url !== `${plan.path}/${accepted.batch_id}` ||
    !("files" in accepted) ||
    !Array.isArray(accepted.files) ||
    accepted.files.length !== plan.fileIds.length
  ) {
    return false;
  }
  for (const [ordinal, value] of accepted.files.entries()) {
    const item: unknown = value;
    if (
      !item ||
      typeof item !== "object" ||
      !("ordinal" in item) ||
      item.ordinal !== ordinal ||
      !("staged_id" in item) ||
      item.staged_id !== plan.fileIds[ordinal] ||
      !("name" in item) ||
      typeof item.name !== "string" ||
      item.name.length === 0 ||
      item.name === "." ||
      item.name === ".." ||
      item.name.includes("/") ||
      item.name.includes("\\") ||
      !("size" in item) ||
      typeof item.size !== "number" ||
      item.size < 0 ||
      !("sha256" in item) ||
      typeof item.sha256 !== "string" ||
      !/^[0-9a-f]{64}$/.test(item.sha256) ||
      !("content_type" in item) ||
      typeof item.content_type !== "string" ||
      item.content_type.length === 0
    ) {
      return false;
    }
  }
  return true;
}

export function confirmUploadAcceptance(
  status: number,
  responseText: string,
  agentId: number,
  plan: UploadBatchPlan,
): DeliveredUploadAcceptance {
  if (status !== 202) throw new Error(failureDetail(status, responseText));
  const accepted: unknown = JSON.parse(responseText);
  if (!isConfirmedUploadAcceptance(accepted, agentId, plan)) {
    throw new Error(UNCONFIRMED_ACCEPTANCE);
  }
  return accepted;
}

export function uploadPercent(progress: { lengthComputable: boolean; loaded: number; total: number }): number | null {
  if (!progress.lengthComputable) return null;
  return Math.round((progress.loaded / progress.total) * 100);
}
