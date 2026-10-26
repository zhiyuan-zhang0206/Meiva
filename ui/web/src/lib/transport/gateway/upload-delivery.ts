import type { DeliveredUploadAcceptance, UploadedBatch } from "../../contracts/types";
import type { Http } from "./http";
import { keyedHeaders } from "./plans";
import { confirmUploadAcceptance, legacyUploadResult, planUploadBatch, uploadPercent } from "./plans";

interface UploadExchange<T> {
  path: string;
  headers: Record<string, string>;
  body: FormData;
  onProgress?: (pct: number) => void;
  settle: (status: number, responseText: string) => T;
  networkError: string;
}

function exchange<T>(http: Http, request: UploadExchange<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = http.openUpload();
    xhr.open("POST", http.url(request.path));
    xhr.withCredentials = true;
    for (const [name, value] of Object.entries(request.headers)) xhr.setRequestHeader(name, value);
    xhr.upload.addEventListener("progress", (event) => {
      const percent = uploadPercent(event);
      if (percent !== null) request.onProgress?.(percent);
    });
    xhr.onload = () => {
      try {
        resolve(request.settle(xhr.status, xhr.responseText));
      } catch (error) {
        reject(error instanceof Error ? error : new Error(request.networkError));
      }
    };
    xhr.onerror = () => reject(new Error(request.networkError));
    xhr.send(request.body);
  });
}

export function submitUploadedFiles(
  http: Http,
  agentId: number,
  files: File[],
  operationKey: string,
  onProgress?: (pct: number) => void,
): Promise<DeliveredUploadAcceptance> {
  const plan = planUploadBatch(agentId, files, operationKey);
  const body = new FormData();
  for (const file of files) body.append("files", file, file.name);
  return exchange(http, {
    path: plan.path,
    headers: plan.headers,
    body,
    onProgress,
    settle: (status, responseText) => confirmUploadAcceptance(status, responseText, agentId, plan),
    networkError: "Upload source acceptance is unconfirmed",
  });
}

export function uploadFiles(
  http: Http,
  agentId: number,
  files: File[],
  onProgress: ((pct: number) => void) | undefined,
  operationKey: string,
): Promise<UploadedBatch> {
  const headers = keyedHeaders(operationKey);
  const body = new FormData();
  for (const file of files) body.append("files", file, file.name);
  return exchange(http, {
    path: `/api/agents/${agentId}/uploads`,
    headers,
    body,
    onProgress,
    settle: legacyUploadResult,
    networkError: "Upload failed",
  });
}
