// meiva:note
// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { startGateway, type GatewayReply, type GatewayRequest, type TestGateway } from "./tests/gateway-server";
import { FakeUpload } from "./tests/fake-upload";
import { httpRig, sequentialKeys, steppedClock, type HttpRig } from "./tests/http-rig";
import { realNetwork } from "./tests/real-network";
import type { UploadRequest } from "../transport";

const batchId = "a".repeat(32);
const stagedId = "c".repeat(32);

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

const deliveryStatus = () => ({
  batch_id: batchId,
  state: "pending" as const,
  reason: null,
  directory: `/Users/test/.meiva/uploads/${batchId}`,
  message_text: "hello",
  files: acceptance().files,
  inbound_id: null,
  source: { machine: "m1", home: "/tmp" },
  target: { machine: "m1", home: "/tmp" },
  attempts: 0,
  next_attempt_at: null,
});

const STAGED: GatewayReply = { kind: "json", status: 201, body: stagedReceipt() };
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
  answer = STAGED;
  gateway = await startGateway(realNetwork.createServer, () => answer);
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

describe("stageUploadFile", () => {
  it("stages one file to the guarded stage path and completes at 201", async () => {
    const result = await rig.api.stageUploadFile(7, newFile(), "stage-intent");
    expect(result).toEqual(stagedReceipt());
    const request = only(gateway.requests);
    expect([request.method, request.path]).toEqual(["POST", "/api/keyed/v1/agents/7/uploads/staged"]);
    expect(request.headers["idempotency-key"]).toBe("stage-intent");
    expect(request.headers["idempotency-scope"]).toBe("principal-v1");
    expect(request.headers["content-type"]).toMatch(/^multipart\/form-data; boundary=/);
    expect(request.body).toContain('name="file"; filename="report.txt"');
    expect(request.body).toContain("hello");
    expect(opened).toHaveLength(1);
    expect(opened[0].withCredentials).toBe(true);
  });

  it("reports upload progress as a whole percentage", async () => {
    const fake = new FakeUpload();
    rig = rigOver(() => fake);
    const percentages: number[] = [];
    const result = rig.api.stageUploadFile(7, newFile(), "stage-intent", (pct) => percentages.push(pct));
    fake.progress(5, 5, true);
    fake.progress(1, 3, false);
    fake.finish(201, JSON.stringify(stagedReceipt()));
    await result;
    expect(percentages).toEqual([100]);
    expect(fake.opened).toEqual(["POST", `${gateway.origin}/api/keyed/v1/agents/7/uploads/staged`]);
    expect(fake.headers).toEqual([
      ["Idempotency-Key", "stage-intent"],
      ["Idempotency-Scope", "principal-v1"],
    ]);
  });

  it("aborts an in-flight stage when the caller's signal aborts", async () => {
    const fake = new FakeUpload();
    rig = rigOver(() => fake);
    const controller = new AbortController();
    const result = rig.api.stageUploadFile(7, newFile(), "stage-intent", undefined, controller.signal);
    controller.abort();
    await expect(result).rejects.toThrow("aborted");
    expect(fake.aborted).toBe(true);
  });

  it("does not open an upload when the signal is already aborted", async () => {
    const fake = new FakeUpload();
    rig = rigOver(() => fake);
    const controller = new AbortController();
    controller.abort();
    await expect(
      rig.api.stageUploadFile(7, newFile(), "stage-intent", undefined, controller.signal),
    ).rejects.toThrow("aborted");
    expect(fake.opened).toBeNull();
  });

  it.each([200, 404, 409, 503])("never falls back or resubmits on HTTP %i", async (status) => {
    answer = { kind: "json", status, body: stagedReceipt() };
    await expect(rig.api.stageUploadFile(7, newFile(), "stage-intent")).rejects.toThrow(`HTTP ${status}`);
    expect(gateway.requests).toHaveLength(1);
    expect(opened).toHaveLength(1);
  });
});

describe("deleteStagedUploadFile", () => {
  it("sends DELETE to the staged file path", async () => {
    answer = { kind: "text", status: 204, body: "" };
    await rig.api.deleteStagedUploadFile(7, stagedId);
    const request = only(gateway.requests);
    expect([request.method, request.path]).toEqual(["DELETE", `/api/keyed/v1/agents/7/uploads/staged/${stagedId}`]);
  });
});

describe("submitUploadedFiles", () => {
  beforeEach(() => {
    answer = ACCEPTED;
  });

  it("submits staged file IDs and message text once to the guarded send path", async () => {
    const result = await rig.api.submitUploadedFiles(7, [stagedId], "hello", "original-intent");
    expect(result).toEqual(acceptance());
    const request = only(gateway.requests);
    expect([request.method, request.path]).toEqual(["POST", "/api/keyed/v1/agents/7/uploads"]);
    expect(request.headers["idempotency-key"]).toBe("original-intent");
    expect(request.headers["idempotency-scope"]).toBe("principal-v1");
    expect(JSON.parse(request.body)).toEqual({ file_ids: [stagedId], text: "hello" });
  });

  it.each([200, 404, 409, 503])("never falls back or resubmits on HTTP %i", async (status) => {
    answer = { kind: "json", status, body: acceptance() };
    await expect(rig.api.submitUploadedFiles(7, [stagedId], "hello", "original-intent")).rejects.toThrow(
      `HTTP ${status}`,
    );
    expect(gateway.requests).toHaveLength(1);
  });

  it("preserves the explicit identity for a deliberate same-intent resubmission", async () => {
    answer = { kind: "drop" };
    await expect(rig.api.submitUploadedFiles(7, [stagedId], "hello", "original-intent")).rejects.toThrow(
      "unconfirmed",
    );
    answer = ACCEPTED;
    await expect(rig.api.submitUploadedFiles(7, [stagedId], "hello", "original-intent")).resolves.toEqual(
      acceptance(),
    );
    expect(gateway.requests.map((request) => request.headers["idempotency-key"])).toEqual([
      "original-intent",
      "original-intent",
    ]);
  });

  it("rejects a source receipt for another agent", async () => {
    answer = { kind: "json", status: 202, body: { ...acceptance(), agent_id: 8 } };
    await expect(rig.api.submitUploadedFiles(7, [stagedId], "hello", "original-intent")).rejects.toThrow(
      "unconfirmed",
    );
  });

  it("rejects invalid identity and empty batches before transport effects", () => {
    expect(() => rig.api.submitUploadedFiles(0, [stagedId], "", "key")).toThrow("agent ID");
    expect(() => rig.api.submitUploadedFiles(7, [stagedId], "", "")).toThrow("idempotency key");
    expect(() => rig.api.submitUploadedFiles(7, [stagedId], "", "x".repeat(129))).toThrow("idempotency key");
    expect(() => rig.api.submitUploadedFiles(7, [], "", "key")).toThrow("contain files");
    expect(gateway.requests).toHaveLength(0);
  });
});

describe("getUploadDeliveryStatus", () => {
  it("reads the batch delivery status from the keyed status endpoint", async () => {
    answer = { kind: "json", status: 200, body: deliveryStatus() };
    const status = await rig.api.getUploadDeliveryStatus(7, batchId);
    expect(status).toEqual(deliveryStatus());
    const request = only(gateway.requests);
    expect([request.method, request.path]).toEqual(["GET", `/api/keyed/v1/agents/7/uploads/${batchId}`]);
  });
});
