import type { DeliveredUploadAcceptance, UploadedBatch } from "../../../contracts/types";
import { keyedHeaders } from "./key-plan";

interface SubmittedFile {
  name: string;
  size: number;
  contentType: string;
}

export interface UploadBatchPlan {
  path: string;
  headers: Record<string, string>;
  submitted: readonly SubmittedFile[];
}

export function planUploadBatch(
  agentId: number,
  files: readonly { name: string; size: number; type: string }[],
  operationKey: string,
): UploadBatchPlan {
  if (!Number.isSafeInteger(agentId) || agentId <= 0) throw new Error("invalid upload agent ID");
  const headers = keyedHeaders(operationKey);
  if (files.length === 0) throw new Error("upload batch must contain files");
  return {
    path: `/api/keyed/v1/agents/${agentId}/uploads`,
    headers,
    submitted: files.map((file) => ({
      name: file.name,
      size: file.size,
      contentType: file.type || "application/octet-stream",
    })),
  };
}

function failureDetail(status: number, responseText: string): string {
  let detail = `HTTP ${status}`;
  try {
    const error: unknown = JSON.parse(responseText);
    if (error && typeof error === "object" && "detail" in error && typeof error.detail === "string") {
      detail = error.detail;
    }
  } catch {
    detail = `HTTP ${status}`;
  }
  return detail;
}

const UNCONFIRMED = "Upload source acceptance is unconfirmed";

export function confirmUploadAcceptance(
  status: number,
  responseText: string,
  agentId: number,
  plan: UploadBatchPlan,
): DeliveredUploadAcceptance {
  if (status !== 202) throw new Error(failureDetail(status, responseText));
  const accepted: unknown = JSON.parse(responseText);
  if (!accepted || typeof accepted !== "object" || !("batch_id" in accepted) ||
    typeof accepted.batch_id !== "string" || !/^[0-9a-f]{32}$/.test(accepted.batch_id) ||
    !("agent_id" in accepted) || accepted.agent_id !== agentId ||
    !("status_url" in accepted) || accepted.status_url !== `${plan.path}/${accepted.batch_id}` ||
    !("files" in accepted) || !Array.isArray(accepted.files) || accepted.files.length !== plan.submitted.length) {
    throw new Error(UNCONFIRMED);
  }
  for (const [ordinal, value] of accepted.files.entries()) {
    const item: unknown = value;
    if (!item || typeof item !== "object" || !("ordinal" in item) || item.ordinal !== ordinal ||
      !("filename" in item) || item.filename !== plan.submitted[ordinal].name ||
      !("size" in item) || item.size !== plan.submitted[ordinal].size ||
      !("sha256" in item) || typeof item.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(item.sha256) ||
      !("name" in item) || typeof item.name !== "string" ||
      item.name.split(".", 1)[0] !== `${accepted.batch_id}-${ordinal}` ||
      !("content_type" in item) || item.content_type !== plan.submitted[ordinal].contentType) {
      throw new Error(UNCONFIRMED);
    }
  }
  return accepted as DeliveredUploadAcceptance;
}

export function legacyUploadResult(status: number, responseText: string): UploadedBatch {
  if (status >= 200 && status < 300) return JSON.parse(responseText) as UploadedBatch;
  let message = `HTTP ${status}`;
  try {
    const err = JSON.parse(responseText) as { detail?: string };
    if (err.detail) message = err.detail;
  } catch { message = `HTTP ${status}`; }
  throw new Error(message);
}

export function uploadPercent(progress: { lengthComputable: boolean; loaded: number; total: number }): number | null {
  if (!progress.lengthComputable) return null;
  return Math.round((progress.loaded / progress.total) * 100);
}
