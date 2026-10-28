import { expect, it, vi } from "vitest";

import { createLazyChunk } from "./lazy-chunk";

it("peek is null until the import resolves, then returns the resolved value", async () => {
  let resolveImport: (value: string) => void = () => undefined;
  const pending = new Promise<string>((resolve) => {
    resolveImport = resolve;
  });
  const chunk = createLazyChunk({ name: "test", importModule: () => pending });

  expect(chunk.peek()).toBeNull();
  const loading = chunk.load();
  expect(chunk.peek()).toBeNull();

  resolveImport("renderer");
  await expect(loading).resolves.toBe("renderer");
  expect(chunk.peek()).toBe("renderer");
});

it("shares one import attempt between concurrent callers", async () => {
  const importModule = vi.fn(() => Promise.resolve("renderer"));
  const chunk = createLazyChunk({ name: "test", importModule });

  const [first, second, third] = await Promise.all([chunk.load(), chunk.preload(), chunk.load()]);

  expect(first).toBe("renderer");
  expect(second).toBeUndefined();
  expect(third).toBe("renderer");
  expect(importModule).toHaveBeenCalledTimes(1);
});

it("does not import again once the value resolved", async () => {
  const importModule = vi.fn(() => Promise.resolve("renderer"));
  const chunk = createLazyChunk({ name: "test", importModule });

  await chunk.preload();
  await chunk.preload();

  expect(importModule).toHaveBeenCalledTimes(1);
  expect(await chunk.load()).toBe("renderer");
  expect(importModule).toHaveBeenCalledTimes(1);
});

it("reports once for a failed attempt, drops it, and retries on the next call", async () => {
  const report = vi.fn();
  let attempts = 0;
  const chunk = createLazyChunk({
    name: "test",
    report,
    importModule: () => {
      attempts += 1;
      return attempts === 1 ? Promise.reject(new Error("chunk load failed")) : Promise.resolve("renderer");
    },
  });

  await expect(chunk.load()).rejects.toThrow("chunk load failed");
  expect(report).toHaveBeenCalledTimes(1);
  expect(report).toHaveBeenCalledWith("[test] failed to load chunk: chunk load failed");
  expect(chunk.peek()).toBeNull();

  await expect(chunk.load()).resolves.toBe("renderer");
  expect(attempts).toBe(2);
  expect(chunk.peek()).toBe("renderer");
  expect(report).toHaveBeenCalledTimes(1);
});

it("preload never raises the failure it swallowed", async () => {
  const report = vi.fn();
  const chunk = createLazyChunk({
    name: "test",
    report,
    importModule: () => Promise.reject(new Error("chunk load failed")),
  });

  await expect(chunk.preload()).resolves.toBeUndefined();
  expect(report).toHaveBeenCalledTimes(1);
});
