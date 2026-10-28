// meiva:note
// The row canvases skip a paint when their key is unchanged, so the key carries every input the painters
// read: these tests change one input at a time and fail if one stops reaching it.
// meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md

import { describe, expect, it } from "vitest";

import type { RowDeco } from "./run-timeline-paint";
import { geometryKey, rowPaintKey } from "./paint-key";

const deco = (over: Partial<RowDeco> = {}): RowDeco => ({
  primaryKey: null,
  linkedKeys: new Set<string>(),
  primary: null,
  linked: null,
  lineX: null,
  ...over,
});

type PaintParts = Parameters<typeof rowPaintKey>[0];

const base: PaintParts = {
  layout: {},
  selection: null,
  hover: null,
  highlight: null,
  deco: deco(),
  width: 1000,
  height: 40,
  theme: 0,
  extra: "nodes",
};

const key = (over: Partial<PaintParts> = {}) => rowPaintKey({ ...base, ...over });

describe("rowPaintKey", () => {
  it("is unchanged when nothing visible changed", () => {
    expect(key()).toBe(key());
  });

  it("changes for every input a painter reads", () => {
    const variants: Partial<PaintParts>[] = [
      { layout: {} },
      { selection: { kind: "node", id: "1" } },
      { hover: { kind: "node", id: "2" } },
      { highlight: { cls: "call", source: null } },
      { deco: deco({ primaryKey: "n1" }) },
      { deco: deco({ linkedKeys: new Set(["n2"]) }) },
      { deco: deco({ primary: { left: 1, width: 2 } }) },
      { deco: deco({ linked: { left: 3, width: 4 } }) },
      { deco: deco({ lineX: 5 }) },
      { width: 900 },
      { height: 32 },
      { theme: 1 },
      { extra: "tokens:0.5" },
    ];
    const keys = new Set(variants.map((variant) => key(variant)));
    expect(keys.size).toBe(variants.length);
    for (const value of keys) expect(value).not.toBe(key());
  });

  it("distinguishes a unit selection by its message range, not only its kind", () => {
    expect(key({ selection: { kind: "unit", i0: 1, i1: 2, unitKind: "text" } })).not.toBe(
      key({ selection: { kind: "unit", i0: 1, i1: 3, unitKind: "text" } }),
    );
  });
});

describe("geometryKey", () => {
  it("changes when one of the rows is laid out again", () => {
    const first = {};
    const second = {};
    expect(geometryKey([first, second])).toBe(geometryKey([first, second]));
    expect(geometryKey([first, second])).not.toBe(geometryKey([{}, second]));
    expect(geometryKey([])).toBe("");
  });
});
