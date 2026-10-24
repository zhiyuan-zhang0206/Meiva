import { describe, expect, it } from "vitest";

import { confirmUploadAcceptance, legacyUploadResult, planUploadBatch, uploadPercent } from "./upload-plan";

const batchId = "a".repeat(32);
const file = { name: "report.txt", size: 5, type: "text/plain" };
const acceptance = () => ({
  batch_id: batchId,
  agent_id: 7,
  status_url: `/api/keyed/v1/agents/7/uploads/${batchId}`,
  files: [{ ordinal: 0, filename: "report.txt", name: `${batchId}-0.txt`, size: 5,
    sha256: "b".repeat(64), content_type: "text/plain" }],
});

describe("upload batch plan", () => {
  it("names the guarded path, the keyed headers and what was submitted", () => {
    expect(planUploadBatch(7, [file], "original-intent")).toEqual({
      path: "/api/keyed/v1/agents/7/uploads",
      headers: { "Idempotency-Key": "original-intent", "Idempotency-Scope": "principal-v1" },
      submitted: [{ name: "report.txt", size: 5, contentType: "text/plain" }],
    });
  });

  it("matches the multipart default for a File without a MIME type", () => {
    expect(planUploadBatch(7, [{ ...file, type: "" }], "k").submitted[0].contentType).toBe("application/octet-stream");
  });

  it("rejects invalid identity and empty batches before transport effects", () => {
    expect(() => planUploadBatch(0, [file], "key")).toThrow("agent ID");
    expect(() => planUploadBatch(1.5, [file], "key")).toThrow("agent ID");
    expect(() => planUploadBatch(7, [file], "")).toThrow("idempotency key");
    expect(() => planUploadBatch(7, [file], "x".repeat(129))).toThrow("idempotency key");
    expect(() => planUploadBatch(7, [], "key")).toThrow("contain files");
  });
});

describe("delivered upload source acceptance", () => {
  const plan = planUploadBatch(7, [file], "original-intent");

  it("completes at a 202 receipt that matches what was submitted", () => {
    expect(confirmUploadAcceptance(202, JSON.stringify(acceptance()), 7, plan)).toEqual(acceptance());
  });

  it.each([200, 404, 409, 503])("never treats HTTP %i as acceptance", (status) => {
    expect(() => confirmUploadAcceptance(status, JSON.stringify(acceptance()), 7, plan)).toThrow(`HTTP ${status}`);
  });

  it("surfaces the server's detail for a refusal", () => {
    expect(() => confirmUploadAcceptance(409, JSON.stringify({ detail: "agent is busy" }), 7, plan))
      .toThrow("agent is busy");
  });

  it.each([
    { ...acceptance(), agent_id: 8 },
    { ...acceptance(), status_url: "/api/agents/7/uploads" },
    { ...acceptance(), files: [] },
    { ...acceptance(), files: [{ ...acceptance().files[0], filename: "other.txt" }] },
    { ...acceptance(), files: [{ ...acceptance().files[0], size: 4 }] },
    { ...acceptance(), files: [{ ...acceptance().files[0], content_type: "image/png" }] },
    { ...acceptance(), files: [{ ...acceptance().files[0], name: `${batchId}-00.txt` }] },
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

describe("legacy upload result", () => {
  it("parses a 2xx body", () => {
    expect(legacyUploadResult(200, '{"files":[]}')).toEqual({ files: [] });
  });

  it("rejects on HTTP 404 with detail", () => {
    expect(() => legacyUploadResult(404, JSON.stringify({ detail: "Agent not found" }))).toThrow("Agent not found");
  });

  it("rejects with HTTP status when no detail", () => {
    expect(() => legacyUploadResult(500, "boom")).toThrow("HTTP 500");
    expect(() => legacyUploadResult(500, "{}")).toThrow("HTTP 500");
  });

  it("rejects on invalid JSON error body", () => {
    expect(() => legacyUploadResult(400, "not json")).toThrow("HTTP 400");
  });

  it("rejects a 2xx body that is not JSON instead of escaping the load handler", () => {
    expect(() => legacyUploadResult(200, "<html>")).toThrow(SyntaxError);
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
