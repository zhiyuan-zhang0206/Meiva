import type {
  DeliveredUploadAcceptance,
  DeliveredUploadStatus,
  StagedFile,
} from "../../contracts/types";
import type { Http } from "./http";
import {
  confirmStagedUpload,
  confirmUploadAcceptance,
  planStagedUpload,
  planUploadBatch,
  uploadPercent,
} from "./plans";

interface UploadExchange<T> {
  path: string;
  headers: Record<string, string>;
  body: FormData;
  onProgress?: (pct: number) => void;
  signal?: AbortSignal;
  settle: (status: number, responseText: string) => T;
  networkError: string;
}

function exchange<T>(http: Http, request: UploadExchange<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    if (request.signal?.aborted) {
      reject(new DOMException("Upload aborted", "AbortError"));
      return;
    }
    const xhr = http.openUpload();
    const onCallerAbort = () => {
      xhr.abort();
    };
    request.signal?.addEventListener("abort", onCallerAbort, { once: true });
    const cleanup = () => {
      request.signal?.removeEventListener("abort", onCallerAbort);
    };
    xhr.open("POST", http.url(request.path));
    xhr.withCredentials = true;
    for (const [name, value] of Object.entries(request.headers)) xhr.setRequestHeader(name, value);
    xhr.upload.addEventListener("progress", (event) => {
      const percent = uploadPercent(event);
      if (percent !== null) request.onProgress?.(percent);
    });
    xhr.onload = () => {
      cleanup();
      try {
        resolve(request.settle(xhr.status, xhr.responseText));
      } catch (error) {
        reject(error instanceof Error ? error : new Error(request.networkError));
      }
    };
    xhr.onerror = () => {
      cleanup();
      reject(new Error(request.networkError));
    };
    xhr.onabort = () => {
      cleanup();
      reject(new DOMException("Upload aborted", "AbortError"));
    };
    xhr.send(request.body);
  });
}

export function stageUploadFile(
  http: Http,
  agentId: number,
  file: File,
  operationKey: string,
  onProgress?: (pct: number) => void,
  signal?: AbortSignal,
): Promise<StagedFile> {
  const plan = planStagedUpload(agentId, file, operationKey);
  const body = new FormData();
  body.append("file", file, file.name);
  return exchange(http, {
    path: plan.path,
    headers: plan.headers,
    body,
    onProgress,
    signal,
    settle: (status, responseText) => confirmStagedUpload(status, responseText, plan),
    networkError: "Staged upload receipt is unconfirmed",
  });
}

export function deleteStagedUploadFile(
  http: Http,
  agentId: number,
  fileId: string,
): Promise<void> {
  if (!Number.isSafeInteger(agentId) || agentId <= 0) throw new Error("invalid upload agent ID");
  if (!/^[0-9a-f]{32}$/.test(fileId)) throw new Error("invalid staged file ID");
  return http
    .send(`/api/keyed/v1/agents/${agentId}/uploads/staged/${fileId}`, { method: "DELETE" })
    .then((res) => {
      if (res.status === 204) return undefined;
      return http.ok<unknown>(res).then(() => undefined);
    });
}

export function submitUploadedFiles(
  http: Http,
  agentId: number,
  fileIds: readonly string[],
  text: string,
  operationKey: string,
): Promise<DeliveredUploadAcceptance> {
  const plan = planUploadBatch(agentId, fileIds, text, operationKey);
  return http
    .send(plan.path, {
      method: "POST",
      headers: plan.headers,
      body: plan.body,
    })
    .then(
      (res) =>
        res.text().then((text) => confirmUploadAcceptance(res.status, text, agentId, plan)),
      () => {
        throw new Error("Upload source acceptance is unconfirmed");
      },
    );
}

export function getUploadDeliveryStatus(
  http: Http,
  agentId: number,
  batchId: string,
  signal?: AbortSignal,
): Promise<DeliveredUploadStatus> {
  if (!Number.isSafeInteger(agentId) || agentId <= 0) throw new Error("invalid upload agent ID");
  if (!/^[0-9a-f]{32}$/.test(batchId)) throw new Error("invalid upload batch ID");
  return http
    .send(`/api/keyed/v1/agents/${agentId}/uploads/${batchId}`, { signal })
    .then(http.ok<DeliveredUploadStatus>);
}
