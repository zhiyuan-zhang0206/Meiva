"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { StagedAttachment } from "@/components/conversation/composer-helpers";
import {
  clearActiveUploadBatch,
  readActiveUploadBatch,
  writeActiveUploadBatch,
} from "@/components/conversation/composer-session";
import type { BrowserObjectUrls, BrowserStorage } from "@/lib/browser/browser";
import { errMsg } from "@/lib/contracts/errors";
import type {
  DeliveredUploadAcceptance,
  DeliveredUploadStatus,
} from "@/lib/contracts/types";
import { isRasterImageType } from "@/lib/format/utils";
import type { Api } from "@/lib/transport/api";

export interface StageQueueBatch {
  readonly seq: number;
  readonly files: readonly File[];
}

const POLL_INTERVAL_MS = 350;

function initialPendingStatus(
  accepted: DeliveredUploadAcceptance,
  messageText: string,
): DeliveredUploadStatus {
  return {
    batch_id: accepted.batch_id,
    state: "pending",
    reason: null,
    directory: accepted.directory,
    message_text: messageText,
    files: accepted.files,
    inbound_id: null,
    source: { machine: "", home: "" },
    target: { machine: "", home: "" },
    attempts: 0,
    next_attempt_at: null,
  };
}

export function useComposerUploads({
  agentId,
  client,
  session,
  objectUrls,
  composerLocked,
  stageQueue,
  onUploadFiles,
  onDeliveringBatchChange,
  onUploadSent,
  onUploadError,
}: {
  agentId: number | null;
  client: Api;
  session: BrowserStorage;
  objectUrls: BrowserObjectUrls;
  composerLocked: boolean;
  stageQueue?: StageQueueBatch | null;
  onUploadFiles?: (files: File[]) => void;
  onDeliveringBatchChange?: (batch: DeliveredUploadStatus | null) => void;
  onUploadSent?: () => void;
  onUploadError?: (msg: string) => void;
}) {
  const [attachments, setAttachments] = useState<StagedAttachment[]>([]);
  const attachmentsRef = useRef<StagedAttachment[]>(attachments);
  useEffect(() => {
    attachmentsRef.current = attachments;
  }, [attachments]);

  const [activeBatchId, setActiveBatchId] = useState<string | null>(() =>
    agentId == null ? null : readActiveUploadBatch(session, agentId),
  );
  const [activeBatchStatus, setActiveBatchStatus] = useState<DeliveredUploadStatus | null>(null);
  const prevAgentIdRef = useRef(agentId);
  const imagePreviewsRef = useRef(new Map<string, string>());
  const stageControllersRef = useRef(new Map<string, AbortController>());
  const restoredFileIdsRef = useRef(new Set<string>());

  useEffect(() => {
    for (const [key, previewUrl] of imagePreviewsRef.current) {
      if (!attachments.some((att) => att.key === key)) {
        objectUrls.revoke(previewUrl);
        imagePreviewsRef.current.delete(key);
      }
    }
  }, [attachments, objectUrls]);

  useEffect(() => {
    const previews = imagePreviewsRef.current;
    const controllers = stageControllersRef.current;
    return () => {
      for (const controller of controllers.values()) controller.abort();
      controllers.clear();
      for (const previewUrl of previews.values()) objectUrls.revoke(previewUrl);
      previews.clear();
    };
  }, [objectUrls]);

  const runStageUpload = useCallback(
    (key: string, targetAgentId: number | null, file: File, operationKey: string) => {
      const stageAgentId = targetAgentId ?? 1;
      const controller = new AbortController();
      stageControllersRef.current.set(key, controller);
      client
        .stageUploadFile(
          stageAgentId,
          file,
          operationKey,
          (pct) => {
            setAttachments((prev) =>
              prev.map((att) =>
                att.key === key && att.agentId === targetAgentId ? { ...att, progress: pct } : att,
              ),
            );
          },
          controller.signal,
        )
        .then((staged) => {
          stageControllersRef.current.delete(key);
          const stillPresent =
            prevAgentIdRef.current === targetAgentId &&
            attachmentsRef.current.some((att) => att.key === key);
          if (!stillPresent) {
            void client.deleteStagedUploadFile(stageAgentId, staged.file_id).catch(() => undefined);
            return;
          }
          setAttachments((prev) =>
            prev.map((att) =>
              att.key === key
                ? {
                    ...att,
                    status: "ready" as const,
                    progress: 100,
                    fileId: staged.file_id,
                    name: staged.name,
                    size: staged.size,
                    contentType: staged.content_type,
                    error: null,
                  }
                : att,
            ),
          );
        })
        .catch((err: unknown) => {
          stageControllersRef.current.delete(key);
          if (controller.signal.aborted) return;
          const message = errMsg(err);
          setAttachments((prev) =>
            prev.map((att) =>
              att.key === key ? { ...att, status: "error" as const, error: message } : att,
            ),
          );
        });
    },
    [client],
  );

  const stageFiles = useCallback(
    (files: readonly File[]) => {
      if (composerLocked || files.length === 0) return;
      if (onUploadFiles) {
        onUploadFiles([...files]);
        return;
      }
      for (const file of files) {
        const operationKey = client.newOperationKey();
        const previewUrl = isRasterImageType(file.type) ? objectUrls.create(file) : null;
        const key = previewUrl ?? `att-${operationKey}`;
        if (previewUrl !== null) imagePreviewsRef.current.set(key, previewUrl);
        const entry: StagedAttachment = {
          key,
          agentId,
          file,
          name: file.name,
          size: file.size,
          contentType: file.type || "application/octet-stream",
          previewUrl,
          operationKey,
          status: "uploading",
          progress: 0,
          fileId: null,
          error: null,
        };
        setAttachments((prev) => [...prev, entry]);
        runStageUpload(key, agentId, file, operationKey);
      }
    },
    [agentId, client, composerLocked, objectUrls, onUploadFiles, runStageUpload],
  );

  const lastStageSeqRef = useRef<number | null>(null);
  useEffect(() => {
    if (!stageQueue || stageQueue.seq === lastStageSeqRef.current) return;
    lastStageSeqRef.current = stageQueue.seq;
    stageFiles(stageQueue.files);
  }, [stageQueue, stageFiles]);

  useEffect(() => {
    const prevId = prevAgentIdRef.current;
    if (prevId !== agentId) {
      for (const controller of stageControllersRef.current.values()) controller.abort();
      stageControllersRef.current.clear();
      for (const att of attachmentsRef.current) {
        if (
          att.status === "ready" &&
          att.fileId &&
          !restoredFileIdsRef.current.has(att.fileId)
        ) {
          void client
            .deleteStagedUploadFile(att.agentId ?? 1, att.fileId)
            .catch(() => undefined);
        }
      }
      restoredFileIdsRef.current.clear();
      setAttachments([]);
      const nextBatch = agentId == null ? null : readActiveUploadBatch(session, agentId);
      setActiveBatchId(nextBatch);
      setActiveBatchStatus(null);
      prevAgentIdRef.current = agentId;
    }
  }, [agentId, client, session]);

  useEffect(() => {
    onDeliveringBatchChange?.(
      activeBatchStatus?.state === "pending" ? activeBatchStatus : null,
    );
  }, [activeBatchStatus, onDeliveringBatchChange]);

  useEffect(() => {
    if (agentId == null || activeBatchId === null || activeBatchStatus?.state === "hold") {
      return;
    }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const status = await client.getUploadDeliveryStatus(
          agentId,
          activeBatchId,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        if (status.state === "accepted") {
          clearActiveUploadBatch(session, agentId);
          setActiveBatchId(null);
          setActiveBatchStatus(null);
          onUploadSent?.();
          return;
        }
        setActiveBatchStatus(status);
        if (status.state === "pending") {
          timer = setTimeout(() => {
            void poll();
          }, POLL_INTERVAL_MS);
        }
      } catch {
        if (controller.signal.aborted) return;
        clearActiveUploadBatch(session, agentId);
        setActiveBatchId(null);
        setActiveBatchStatus(null);
      }
    };
    void poll();
    return () => {
      controller.abort();
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [agentId, activeBatchId, activeBatchStatus?.state, client, onUploadSent, session]);

  const removeAttachment = useCallback(
    (key: string) => {
      if (composerLocked) return;
      const target = attachmentsRef.current.find((att) => att.key === key);
      setAttachments((prev) => prev.filter((att) => att.key !== key));
      const controller = stageControllersRef.current.get(key);
      if (controller) {
        controller.abort();
        stageControllersRef.current.delete(key);
        return;
      }
      if (
        target?.status === "ready" &&
        target.fileId &&
        !restoredFileIdsRef.current.has(target.fileId)
      ) {
        void client
          .deleteStagedUploadFile(target.agentId ?? 1, target.fileId)
          .catch(() => undefined);
      }
    },
    [client, composerLocked],
  );

  const retryAttachment = useCallback(
    (key: string) => {
      if (composerLocked) return;
      const target = attachmentsRef.current.find((att) => att.key === key);
      if (target?.status !== "error" || !target.file) return;
      setAttachments((prev) =>
        prev.map((att) =>
          att.key === key ? { ...att, status: "uploading" as const, progress: 0, error: null } : att,
        ),
      );
      runStageUpload(target.key, target.agentId, target.file, target.operationKey);
    },
    [composerLocked, runStageUpload],
  );

  const clearSubmittedAttachments = useCallback(() => {
    restoredFileIdsRef.current.clear();
    setAttachments([]);
  }, []);

  const recordBatchAccepted = useCallback(
    (accepted: DeliveredUploadAcceptance, messageText: string) => {
      if (agentId != null) {
        writeActiveUploadBatch(session, agentId, accepted.batch_id);
      }
      setActiveBatchId(accepted.batch_id);
      setActiveBatchStatus(initialPendingStatus(accepted, messageText));
      onUploadSent?.();
    },
    [agentId, onUploadSent, session],
  );

  const restoreHeldBatch = useCallback(
    (onRestoreText: (text: string) => void) => {
      if (composerLocked || activeBatchStatus?.state !== "hold") return;
      if (activeBatchStatus.message_text) {
        onRestoreText(activeBatchStatus.message_text);
      }
      const restored: StagedAttachment[] = activeBatchStatus.files.map((f) => {
        restoredFileIdsRef.current.add(f.staged_id);
        return {
          key: `restored-${activeBatchStatus.batch_id}-${f.staged_id}`,
          agentId,
          file: null,
          name: f.name,
          size: f.size,
          contentType: f.content_type,
          previewUrl: null,
          operationKey: f.staged_id,
          status: "ready" as const,
          progress: 100,
          fileId: f.staged_id,
          error: null,
        };
      });
      setAttachments((prev) => [...prev, ...restored]);
      if (agentId != null) clearActiveUploadBatch(session, agentId);
      setActiveBatchId(null);
      setActiveBatchStatus(null);
    },
    [activeBatchStatus, agentId, composerLocked, session],
  );

  const retryHeldBatch = useCallback(async () => {
    if (composerLocked || agentId == null || activeBatchStatus?.state !== "hold") {
      return;
    }
    try {
      const accepted = await client.submitUploadedFiles(
        agentId,
        activeBatchStatus.files.map((f) => f.staged_id),
        activeBatchStatus.message_text,
        client.newOperationKey(),
      );
      recordBatchAccepted(accepted, activeBatchStatus.message_text);
    } catch (e: unknown) {
      onUploadError?.(errMsg(e));
    }
  }, [activeBatchStatus, agentId, client, composerLocked, onUploadError, recordBatchAccepted]);

  const readyFileIds = attachments.flatMap((att) =>
    att.status === "ready" && att.fileId ? [att.fileId] : [],
  );
  const hasPendingStage = attachments.some(
    (att) => att.status === "uploading" || att.status === "error",
  );
  const deliveryInFlight = activeBatchId !== null && activeBatchStatus?.state !== "hold";
  const heldBatch = activeBatchStatus?.state === "hold" ? activeBatchStatus : null;

  return {
    attachments,
    readyFileIds,
    hasPendingStage,
    deliveryInFlight,
    heldBatch,
    stageFiles,
    removeAttachment,
    retryAttachment,
    clearSubmittedAttachments,
    recordBatchAccepted,
    restoreHeldBatch,
    retryHeldBatch,
  };
}
