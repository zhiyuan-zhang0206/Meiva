// meiva:note
// One deferred module, shared: the first caller starts the dynamic import, later callers join that attempt, a resolved value stays readable synchronously so a prefetched chunk paints highlighted on the first render, and a rejected attempt is dropped so the next call retries instead of pinning the fallback for the session. The chunk is a value the component receives; `report` carries where a failed attempt is logged (production logs, a test collects), so a test never resets module state it would leak into the next one.
// meiva:link ui/web/src/docs/frontend-components/code-blocks.meiva.okf.md

import { errMsg } from "@/lib/contracts/errors";

export interface LazyChunk<T> {
  peek(): T | null;
  load(): Promise<T>;
  preload(): Promise<void>;
}

export function createLazyChunk<T>(options: {
  name: string;
  importModule: () => Promise<T>;
  report?: (message: string) => void;
}): LazyChunk<T> {
  const report = options.report ?? ((message: string) => console.warn(message));
  let attempt: Promise<T> | null = null;
  let resolved: T | null = null;

  function load(): Promise<T> {
    if (attempt !== null) return attempt;
    const started: Promise<T> = options
      .importModule()
      .then((value) => {
        if (attempt === started) resolved = value;
        return value;
      })
      .catch((error: unknown) => {
        if (attempt === started) attempt = null;
        report(`[${options.name}] failed to load chunk: ${errMsg(error)}`);
        throw error;
      });
    attempt = started;
    return started;
  }

  return {
    peek: () => resolved,
    load,
    preload: () => load().then(() => undefined, () => undefined),
  };
}
