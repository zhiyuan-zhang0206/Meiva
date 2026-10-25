// meiva:note
// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { startGateway, type GatewayReply, type GatewayRequest, type TestGateway } from "./tests/gateway-server";
import { FakeUpload } from "./tests/fake-upload";
import { httpRig, sequentialKeys, steppedClock, type HttpRig } from "./tests/http-rig";
import { realNetwork } from "./tests/real-network";
import type { UploadRequest } from "../transport";

const batchId = "a".repeat(32);
const acceptance = () => ({
  batch_id: batchId,
  agent_id: 7,
  status_url: `/api/keyed/v1/agents/7/uploads/${batchId}`,
  files: [{ ordinal: 0, filename: "report.txt", name: `${batchId}-0.txt`, size: 5,
    sha256: "b".repeat(64), content_type: "text/plain" }],
});
const ACCEPTED: GatewayReply = { kind: "json", status: 202, body: acceptance() };

let gateway: TestGateway;
let answer: GatewayReply;
let opened: UploadRequest[];
let rig: HttpRig;

function newFile(name = "report.txt", type = "text/plain"): File {
  return new File(["hello"], name, { type });
}

function rigOver(openUpload: () => UploadRequest): HttpRig {
  return httpRig({
    base: gateway.origin,
    fetch: realNetwork.fetch,
    now: steppedClock(1),
    keys: sequentialKeys(),
    openUpload,
  });
}

beforeEach(async () => {
  answer = ACCEPTED;
  gateway = await startGateway(() => answer);
  opened = [];
  rig = rigOver(() => {
    const request = new realNetwork.XMLHttpRequest();
    opened.push(request);
    return request;
  });
});
afterEach(async () => {
  await gateway.close();
});

function only(requests: GatewayRequest[]): GatewayRequest {
  expect(requests).toHaveLength(1);
  return requests[0];
}

describe("delivered upload source acceptance", () => {
  it("submits once to the guarded path and completes at source acceptance", async () => {
    const files = [newFile()];
    const result = rig.api.submitUploadedFiles(7, files, "original-intent");
    files.length = 0;
    await expect(result).resolves.toEqual(acceptance());
    const request = only(gateway.requests);
    expect([request.method, request.path]).toEqual(["POST", "/api/keyed/v1/agents/7/uploads"]);
    expect(request.headers["idempotency-key"]).toBe("original-intent");
    expect(request.headers["idempotency-scope"]).toBe("principal-v1");
    expect(request.headers["content-type"]).toMatch(/^multipart\/form-data; boundary=/);
    expect(request.body).toContain('name="files"; filename="report.txt"');
    expect(request.body).toContain("hello");
    expect(opened).toHaveLength(1);
    expect(opened[0].withCredentials).toBe(true);
  });

  it("reports upload progress as a whole percentage", async () => {
    const fake = new FakeUpload();
    rig = rigOver(() => fake);
    const percentages: number[] = [];
    const result = rig.api.submitUploadedFiles(7, [newFile()], "original-intent", (pct) => percentages.push(pct));
    fake.progress(5, 5, true);
    fake.progress(1, 3, false);
    fake.finish(202, JSON.stringify(acceptance()));
    await result;
    expect(percentages).toEqual([100]);
    expect(fake.opened).toEqual(["POST", `${gateway.origin}/api/keyed/v1/agents/7/uploads`]);
    expect(fake.headers).toEqual([["Idempotency-Key", "original-intent"], ["Idempotency-Scope", "principal-v1"]]);
  });

  it.each([200, 404, 409, 503])("never falls back or resubmits on HTTP %i", async (status) => {
    answer = { kind: "json", status, body: acceptance() };
    await expect(rig.api.submitUploadedFiles(7, [newFile()], "original-intent")).rejects.toThrow(`HTTP ${status}`);
    expect(gateway.requests).toHaveLength(1);
    expect(opened).toHaveLength(1);
  });

  it("preserves the explicit identity for a deliberate same-intent resubmission", async () => {
    answer = { kind: "drop" };
    await expect(rig.api.submitUploadedFiles(7, [newFile()], "original-intent")).rejects.toThrow("unconfirmed");
    expect(opened).toHaveLength(1);
    answer = ACCEPTED;
    await expect(rig.api.submitUploadedFiles(7, [newFile()], "original-intent")).resolves.toEqual(acceptance());
    expect(gateway.requests.map((request) => request.headers["idempotency-key"])).toEqual([
      "original-intent",
      "original-intent",
    ]);
  });

  it("rejects a source receipt for another agent", async () => {
    answer = { kind: "json", status: 202, body: { ...acceptance(), agent_id: 8 } };
    await expect(rig.api.submitUploadedFiles(7, [newFile()], "original-intent")).rejects.toThrow("unconfirmed");
  });

  it("settles malformed JSON without leaving the caller pending", async () => {
    answer = { kind: "text", status: 202, body: "{" };
    await expect(rig.api.submitUploadedFiles(7, [newFile()], "original-intent")).rejects.toThrow();
  });

  it("matches the multipart default for a File without a MIME type", async () => {
    const receipt = acceptance();
    receipt.files[0].content_type = "application/octet-stream";
    answer = { kind: "json", status: 202, body: receipt };
    await expect(rig.api.submitUploadedFiles(7, [newFile("report.txt", "")], "original-intent")).resolves.toEqual(receipt);
  });

  it("rejects invalid identity and empty batches before transport effects", () => {
    expect(() => rig.api.submitUploadedFiles(0, [newFile()], "key")).toThrow("agent ID");
    expect(() => rig.api.submitUploadedFiles(7, [newFile()], "")).toThrow("idempotency key");
    expect(() => rig.api.submitUploadedFiles(7, [newFile()], "x".repeat(129))).toThrow("idempotency key");
    expect(() => rig.api.submitUploadedFiles(7, [], "key")).toThrow("contain files");
    expect(opened).toHaveLength(0);
    expect(gateway.requests).toHaveLength(0);
  });
});

describe("uploadFiles", () => {
  beforeEach(() => {
    answer = { kind: "json", body: { files: [] } };
  });

  it("POSTs one file as FormData to /api/agents/{id}/uploads under a principal-v1 key", async () => {
    answer = {
      kind: "json",
      body: { files: [{ filename: "test.txt", path: "/tmp/test.txt", size: 5, content_type: "text/plain" }] },
    };
    const result = await rig.api.uploadFiles(42, [newFile("test.txt")]);
    expect(result).toEqual({
      files: [{ filename: "test.txt", path: "/tmp/test.txt", size: 5, content_type: "text/plain" }],
    });
    const request = only(gateway.requests);
    expect([request.method, request.path]).toEqual(["POST", "/api/agents/42/uploads"]);
    expect(request.headers["idempotency-key"]).toBe("key-1");
    expect(request.headers["idempotency-scope"]).toBe("principal-v1");
    expect(request.body).toContain('name="files"; filename="test.txt"');
    expect(opened[0].withCredentials).toBe(true);
  });

  it("uses an explicit silent upload identity across invocations without retrying", async () => {
    answer = { kind: "drop" };
    await expect(rig.api.uploadFiles(7, [newFile("photo.png")], undefined, "same-batch")).rejects.toThrow("Upload failed");
    expect(gateway.requests).toHaveLength(1);
    answer = { kind: "json", body: { files: [] } };
    await rig.api.uploadFiles(7, [newFile("photo.png")], undefined, "same-batch");
    expect(gateway.requests.map((request) => request.headers["idempotency-key"])).toEqual(["same-batch", "same-batch"]);
  });

  it("mints one key per invocation", async () => {
    await rig.api.uploadFiles(7, [newFile("photo.png")]);
    await rig.api.uploadFiles(7, [newFile("photo.png")]);
    const [first, second] = gateway.requests.map((request) => request.headers["idempotency-key"]);
    expect(first).toBeTruthy();
    expect(second).not.toBe(first);
  });

  it("fails malformed supplied keys before starting an upload", () => {
    expect(() => rig.api.uploadFiles(7, [newFile("photo.png")], undefined, "")).toThrow("idempotency key");
    expect(() => rig.api.uploadFiles(7, [newFile("photo.png")], undefined, "x".repeat(129))).toThrow("idempotency key");
    expect(opened).toHaveLength(0);
  });

  it("appends every file under the 'files' field for a batch", async () => {
    await rig.api.uploadFiles(7, [newFile("a.txt"), newFile("b.txt"), newFile("c.txt")]);
    const names = [...gateway.requests[0].body.matchAll(/name="files"; filename="([^"]+)"/g)].map((match) => match[1]);
    expect(names).toEqual(["a.txt", "b.txt", "c.txt"]);
  });

  it("calls onProgress when lengthComputable and skips it when not", async () => {
    const fake = new FakeUpload();
    rig = rigOver(() => fake);
    const percentages: number[] = [];
    const result = rig.api.uploadFiles(1, [newFile("x")], (pct) => percentages.push(pct));
    fake.progress(500, 1000, true);
    fake.progress(500, 1000, false);
    fake.finish(200, '{"files":[]}');
    await result;
    expect(percentages).toEqual([50]);
  });

  it("rejects on HTTP 404 with detail", async () => {
    answer = { kind: "json", status: 404, body: { detail: "Agent not found" } };
    await expect(rig.api.uploadFiles(1, [newFile("x")])).rejects.toThrow("Agent not found");
  });

  it("rejects with HTTP status when no detail", async () => {
    answer = { kind: "text", status: 500, body: "boom" };
    await expect(rig.api.uploadFiles(1, [newFile("x")])).rejects.toThrow("HTTP 500");
  });

  it("rejects on network error", async () => {
    answer = { kind: "drop" };
    await expect(rig.api.uploadFiles(1, [newFile("x")])).rejects.toThrow("Upload failed");
  });

  it("rejects on invalid JSON error body", async () => {
    answer = { kind: "text", status: 400, body: "not json" };
    await expect(rig.api.uploadFiles(1, [newFile("x")])).rejects.toThrow("HTTP 400");
  });

  it("rejects instead of hanging when a successful response is not JSON", async () => {
    answer = { kind: "text", status: 200, body: "<html>proxy page</html>" };
    await expect(rig.api.uploadFiles(1, [newFile("x")])).rejects.toBeInstanceOf(SyntaxError);
  });
});
