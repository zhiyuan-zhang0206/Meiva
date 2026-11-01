import { describe, expect, it } from "vitest";

import {
  confirmStagedUpload,
  confirmUploadAcceptance,
  planStagedUpload,
  planUploadBatch,
  uploadPercent,
} from "./upload-plan";

const batchId = "a".repeat(32);
const stagedId = "c".repeat(32);
const file = { name: "report.txt", size: 5, type: "text/plain" };

const stagedReceipt = () => ({
  file_id: stagedId,
  name: "report.txt",
  size: 5,
  content_type: "text/plain",
  sha256: "b".repeat(64),
});

const acceptance = () => ({
  batch_id: batchId,
  agent_id: 7,
  directory: `/Users/test/.meiva/uploads/${batchId}`,
  status_url: `/api/keyed/v1/agents/7/uploads/${batchId}`,
  files: [
    {
      ordinal: 0,
      name: "report.txt",
      staged_id: stagedId,
      size: 5,
      sha256: "b".repeat(64),
      content_type: "text/plain",
    },
  ],
});

describe("staged upload plan", () => {
  it("names the staged path, the keyed headers and what was submitted", () => {
    expect(planStagedUpload(7, file, "stage-intent")).toEqual({
      path: "/api/keyed/v1/agents/7/uploads/staged",
      headers: { "Idempotency-Key": "stage-intent", "Idempotency-Scope": "principal-v1" },
      submitted: { name: "report.txt", size: 5, contentType: "text/plain" },
    });
  });

  it("matches the multipart default for a File without a MIME type", () => {
    expect(planStagedUpload(7, { ...file, type: "" }, "k").submitted.contentType).toBe("application/octet-stream");
  });

  it("rejects invalid identity before transport effects", () => {
    expect(() => planStagedUpload(0, file, "key")).toThrow("agent ID");
    expect(() => planStagedUpload(1.5, file, "key")).toThrow("agent ID");
    expect(() => planStagedUpload(7, file, "")).toThrow("idempotency key");
    expect(() => planStagedUpload(7, file, "x".repeat(129))).toThrow("idempotency key");
  });
});

describe("staged upload confirmation", () => {
  const plan = planStagedUpload(7, file, "stage-intent");

  it("completes at a 201 receipt that matches the staged file", () => {
    expect(confirmStagedUpload(201, JSON.stringify(stagedReceipt()), plan)).toEqual(stagedReceipt());
  });

  it.each([200, 404, 409, 503])("never treats HTTP %i as staged acceptance", (status) => {
    expect(() => confirmStagedUpload(status, JSON.stringify(stagedReceipt()), plan)).toThrow(`HTTP ${status}`);
  });

  it("surfaces the server's detail for a refusal", () => {
    expect(() => confirmStagedUpload(413, JSON.stringify({ detail: "file too large" }), plan)).toThrow(
      "file too large",
    );
  });

  it.each([
    { ...stagedReceipt(), file_id: "short" },
    { ...stagedReceipt(), name: "" },
    { ...stagedReceipt(), size: 4 },
    { ...stagedReceipt(), content_type: "image/png" },
    { ...stagedReceipt(), sha256: "zz" },
    null,
  ])("rejects a mismatched staged receipt", (receipt) => {
    expect(() => confirmStagedUpload(201, JSON.stringify(receipt), plan)).toThrow("unconfirmed");
  });

  it("rejects malformed JSON instead of leaving the caller pending", () => {
    expect(() => confirmStagedUpload(201, "{", plan)).toThrow();
  });
});

describe("upload batch plan", () => {
  it("names the guarded path, the keyed headers and the staged file IDs + message", () => {
    expect(planUploadBatch(7, [stagedId], "hello", "original-intent")).toEqual({
      path: "/api/keyed/v1/agents/7/uploads",
      headers: {
        "content-type": "application/json",
        "Idempotency-Key": "original-intent",
        "Idempotency-Scope": "principal-v1",
      },
      body: JSON.stringify({ file_ids: [stagedId], text: "hello" }),
      fileIds: [stagedId],
    });
  });

  it("rejects invalid identity, invalid staged IDs and empty batches before transport effects", () => {
    expect(() => planUploadBatch(0, [stagedId], "", "key")).toThrow("agent ID");
    expect(() => planUploadBatch(1.5, [stagedId], "", "key")).toThrow("agent ID");
    expect(() => planUploadBatch(7, [stagedId], "", "")).toThrow("idempotency key");
    expect(() => planUploadBatch(7, [stagedId], "", "x".repeat(129))).toThrow("idempotency key");
    expect(() => planUploadBatch(7, [], "", "key")).toThrow("contain files");
    expect(() => planUploadBatch(7, ["not-a-hex-id"], "", "key")).toThrow("staged file ID");
  });
});

describe("delivered upload source acceptance", () => {
  const plan = planUploadBatch(7, [stagedId], "hello", "original-intent");

  it("completes at a 202 receipt that matches what was submitted", () => {
    expect(confirmUploadAcceptance(202, JSON.stringify(acceptance()), 7, plan)).toEqual(acceptance());
  });

  it.each([200, 404, 409, 503])("never treats HTTP %i as acceptance", (status) => {
    expect(() => confirmUploadAcceptance(status, JSON.stringify(acceptance()), 7, plan)).toThrow(`HTTP ${status}`);
  });

  it("surfaces the server's detail for a refusal", () => {
    expect(() => confirmUploadAcceptance(409, JSON.stringify({ detail: "agent is busy" }), 7, plan)).toThrow(
      "agent is busy",
    );
  });

  it.each([
    { ...acceptance(), agent_id: 8 },
    { ...acceptance(), directory: "" },
    { ...acceptance(), status_url: "/api/agents/7/uploads" },
    { ...acceptance(), files: [] },
    { ...acceptance(), files: [{ ...acceptance().files[0], staged_id: "d".repeat(32) }] },
    { ...acceptance(), files: [{ ...acceptance().files[0], name: "../escape.txt" }] },
    { ...acceptance(), files: [{ ...acceptance().files[0], ordinal: 1 }] },
    { ...acceptance(), batch_id: "short" },
    { ...acceptance(), files: [{ ...acceptance().files[0], sha256: "zz" }] },
    null,
  ])("rejects a mismatched source receipt", (receipt) => {
    expect(() => confirmUploadAcceptance(202, JSON.stringify(receipt), 7, plan)).toThrow("unconfirmed");
  });

  it("rejects malformed JSON instead of leaving the caller pending", () => {
    expect(() => confirmUploadAcceptance(202, "{", 7, plan)).toThrow();
  });
});

describe("upload progress", () => {
  it("is a whole percentage when the length is computable", () => {
    expect(uploadPercent({ lengthComputable: true, loaded: 500, total: 1000 })).toBe(50);
    expect(uploadPercent({ lengthComputable: true, loaded: 1, total: 3 })).toBe(33);
    expect(uploadPercent({ lengthComputable: true, loaded: 5, total: 5 })).toBe(100);
  });

  it("is absent when it is not", () => {
    expect(uploadPercent({ lengthComputable: false, loaded: 500, total: 1000 })).toBeNull();
  });
});
