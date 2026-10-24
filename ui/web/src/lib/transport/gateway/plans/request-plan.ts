import { keyedHeaders } from "./key-plan";

export const postInit: RequestInit = Object.freeze({ method: "POST" });

function jsonInit(method: "POST" | "PATCH" | "PUT", body: unknown): RequestInit {
  return {
    method,
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  };
}

export function postJson(body: unknown): RequestInit {
  return jsonInit("POST", body);
}

export function patchJson(body: unknown): RequestInit {
  return jsonInit("PATCH", body);
}

export function putJson(body: unknown): RequestInit {
  return jsonInit("PUT", body);
}

export function keyedJson(body: unknown, operationKey: string, method: "POST" | "PUT" = "POST"): RequestInit {
  return {
    method,
    headers: { "content-type": "application/json", ...keyedHeaders(operationKey) },
    body: JSON.stringify(body),
  };
}

export function keyedPost(operationKey: string): RequestInit {
  return { ...postInit, headers: keyedHeaders(operationKey) };
}
