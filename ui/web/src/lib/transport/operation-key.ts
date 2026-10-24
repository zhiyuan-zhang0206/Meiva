import { operationKeyFrom } from "./gateway/plans";
import { systemKeySource } from "./system-transport";

export function newOperationKey(): string {
  return operationKeyFrom(systemKeySource());
}
