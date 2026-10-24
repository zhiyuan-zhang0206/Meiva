import type { KeySource } from "../../transport";

const MAX_OPERATION_KEY_LENGTH = 128;

export function keyedHeaders(operationKey: string): Record<string, string> {
  if (typeof operationKey !== "string" || !operationKey || operationKey.length > MAX_OPERATION_KEY_LENGTH) {
    throw new Error("idempotency key must contain 1 to 128 characters");
  }
  return { "Idempotency-Key": operationKey, "Idempotency-Scope": "principal-v1" };
}

export function uuid4FromBytes(bytes: Uint8Array): string {
  if (bytes.length !== 16) throw new Error("a UUID needs exactly 16 random bytes");
  const versioned = Uint8Array.from(bytes);
  versioned[6] = (versioned[6] & 0x0f) | 0x40;
  versioned[8] = (versioned[8] & 0x3f) | 0x80;
  const hex = Array.from(versioned, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function operationKeyFrom(source: KeySource): string {
  if (source.randomUUID !== null) return source.randomUUID();
  return uuid4FromBytes(source.getRandomValues(new Uint8Array(16)));
}
