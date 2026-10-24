import { createApi, type Api } from "../api-factory";
import { createHttp, type Http } from "../http";
import type { SlowRequestReport } from "../plans/index";
import type { HttpTransport, KeySource, UploadRequest } from "../../transport";

export interface SentRequest {
  url: string;
  init: RequestInit;
}

export interface HttpRig {
  readonly http: Http;
  readonly api: Api;
  readonly sent: SentRequest[];
  readonly responses: Response[];
  readonly slow: SlowRequestReport[];
}

export interface HttpRigConfig {
  base: string;
  fetch: typeof fetch;
  now: () => number;
  keys: KeySource;
  openUpload: () => UploadRequest;
}

export function sequentialKeys(): KeySource {
  let next = 0;
  return {
    randomUUID: () => `key-${++next}`,
    getRandomValues: (bytes) => bytes,
  };
}

export function keysWithoutRandomUUID(): KeySource {
  let next = 0;
  return {
    randomUUID: null,
    getRandomValues: (bytes) => {
      next += 1;
      bytes.fill(next);
      return bytes;
    },
  };
}

export function steppedClock(step: number): () => number {
  let reading = 0;
  return () => {
    reading += step;
    return reading;
  };
}

export function scriptedClock(readings: readonly number[]): () => number {
  let index = 0;
  return () => {
    if (index >= readings.length) throw new Error("the scripted clock has no more readings");
    return readings[index++];
  };
}

export function noUpload(): UploadRequest {
  throw new Error("this test opened an upload it did not script");
}

// meiva:note
// A gateway client over a real fetch (a local server on port 0 in these tests).
// The rig keeps every request it sent and every response it received, so a test can
// read what the network cannot show (the credentials mode and the abort signal) and
// wait for response headers, plus every slow-request report.
export function httpRig(config: HttpRigConfig): HttpRig {
  const sent: SentRequest[] = [];
  const responses: Response[] = [];
  const slow: SlowRequestReport[] = [];
  const transport: HttpTransport = {
    fetch: (url, init) => {
      sent.push({ url, init });
      return config.fetch(url, init).then((response) => {
        responses.push(response);
        return response;
      });
    },
    now: config.now,
    openUpload: config.openUpload,
    keys: config.keys,
  };
  const http = createHttp(transport, config.base, {
    reportSlow: (report) => {
      slow.push(report);
    },
  });
  return { http, api: createApi(http), sent, responses, slow };
}
