import { describe, expect, it } from "vitest";

import { keyedHeaders, operationKeyFrom, uuid4FromBytes } from "./key-plan";
import type { KeySource } from "../../transport";

function sequentialKeys(): KeySource {
  let next = 0;
  return { randomUUID: () => `key-${++next}`, getRandomValues: (bytes) => bytes };
}

function keysWithoutRandomUUID(): KeySource {
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

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe("keyedHeaders", () => {
  it("pairs the operation key with the principal scope", () => {
    expect(keyedHeaders("intent-1")).toEqual({
      "Idempotency-Key": "intent-1",
      "Idempotency-Scope": "principal-v1",
    });
  });

  it.each(["", "x".repeat(129)])("rejects a key of %j characters before any request is built", (key) => {
    expect(() => keyedHeaders(key)).toThrow("idempotency key must contain 1 to 128 characters");
  });

  it("accepts the 128-character boundary", () => {
    expect(keyedHeaders("x".repeat(128))["Idempotency-Key"]).toHaveLength(128);
  });
});

describe("uuid4FromBytes", () => {
  it("formats sixteen bytes as an RFC 4122 version-4 UUID", () => {
    const zeros = new Uint8Array(16);
    expect(uuid4FromBytes(zeros)).toBe("00000000-0000-4000-8000-000000000000");
    const ones = new Uint8Array(16).fill(0xff);
    expect(uuid4FromBytes(ones)).toBe("ffffffff-ffff-4fff-bfff-ffffffffffff");
  });

  it("leaves the bytes it was given untouched", () => {
    const bytes = new Uint8Array(16).fill(0xab);
    uuid4FromBytes(bytes);
    expect([...bytes]).toEqual(new Array<number>(16).fill(0xab));
  });

  it.each([0, 15, 17])("refuses %i bytes", (length) => {
    expect(() => uuid4FromBytes(new Uint8Array(length))).toThrow("exactly 16 random bytes");
  });
});

describe("operationKeyFrom", () => {
  it("uses randomUUID when the origin offers it", () => {
    expect(operationKeyFrom(sequentialKeys())).toBe("key-1");
  });

  it("falls back to a version-4 UUID from random bytes on a plain-HTTP origin", () => {
    const keys = keysWithoutRandomUUID();
    const first = operationKeyFrom(keys);
    expect(first).toMatch(UUID_V4);
    expect(first).toBe("01010101-0101-4101-8101-010101010101");
    expect(operationKeyFrom(keys)).toBe("02020202-0202-4202-8202-020202020202");
  });
});
